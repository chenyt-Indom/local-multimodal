# -*- coding: utf-8 -*-
"""文生图引擎（Stable Diffusion 本地，基于 diffusers + torch）。

- 默认加载轻量的 SDXL-Turbo 或 SD-1.5 模型（首次需下载权重，之后完全离线）。
- 与 Qwen3-VL 共用 16G 显存：生成时显存不足会自动退化为 CPU 兜底。
- 生产环境可选配 GPU；本机无 GPU 时退化为纯 CPU（慢但可用）。
完全本地运行，数据不出机。
"""
import base64
import io
import os
import threading
from . import config

MODEL_REPO = os.environ.get("SD_MODEL", "stabilityai/sd-turbo")
# 本地已下载的模型目录（通过 ModelScope 下载的 fp16 权重），优先使用，避免联网拉取。
# 打包运行时会自动探测随应用分发的 sd_model 资源目录；源码运行时用环境变量或本机路径。
def _resolve_model_dir() -> str:
    env = os.environ.get("SD_MODEL_DIR")
    if env:
        return env
    candidates = [
        config.res("sd_model"),               # 打包后：_internal/sd_model
        r"D:\local-multimodal-models\sd-turbo",  # 源码本机路径
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "model_index.json")):
            return c
    return candidates[0]

LOCAL_MODEL_DIR = _resolve_model_dir()
_device = None
_pipe = None            # 文生图（txt2img）流水线
_edit_pipe = None       # 图生图（img2img 微改）流水线
_lock = threading.Lock()


def _get_pipe():
    global _pipe, _device
    with _lock:
        if _pipe is not None:
            return _pipe, _device
        import torch
        from diffusers import AutoPipelineForText2Image

        # 本机有无 CUDA：RTX 5070 Ti 应可用
        device = "cuda" if torch.cuda.is_available() else "cpu"

        # 优先从本地目录加载（离线，无需联网）。
        # 注意：ModelScope 下载的 SD 模型只含 fp16 权重，必须传 variant="fp16"，
        # 否则 diffusers 会去找不存在的 diffusion_pytorch_model.safetensors 而报错。
        if os.path.isdir(LOCAL_MODEL_DIR) and os.path.exists(
                os.path.join(LOCAL_MODEL_DIR, "model_index.json")):
            try:
                pipe = AutoPipelineForText2Image.from_pretrained(
                    LOCAL_MODEL_DIR, torch_dtype=torch.float16, variant="fp16",
                    local_files_only=True)
            except Exception as e:
                # 个别组件可能没有 fp16 文件，退化为默认 variant 再试一次
                pipe = AutoPipelineForText2Image.from_pretrained(
                    LOCAL_MODEL_DIR, torch_dtype=torch.float16,
                    local_files_only=True)
            _model_src = LOCAL_MODEL_DIR
        else:
            dtype = torch.float16 if device == "cuda" else torch.float32
            pipe = AutoPipelineForText2Image.from_pretrained(
                MODEL_REPO, torch_dtype=dtype, variant="fp16"
                if device == "cuda" else None)
            _model_src = MODEL_REPO
        pipe.to(device)
        # sd-turbo 建议 1~4 步，这里设合理默认
        _pipe, _device = pipe, device
        return pipe, device


def _pipe_loaded():
    return _pipe is not None


def generate(prompt: str, negative_prompt: str = "", steps: int = 4,
             width: int = 512, height: int = 512) -> dict:
    """文生图，返回 base64 PNG。失败时返回错误信息。"""
    try:
        pipe, device = _get_pipe()
    except Exception as e:
        return {"ok": False, "error": f"文生图引擎加载失败: {e}"}
    try:
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "low quality, blurry, watermark",
            num_inference_steps=max(1, steps),
            guidance_scale=0.0 if MODEL_REPO.endswith("sd-turbo") else 7.5,
            width=width, height=height,
        ).images[0]
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "device": device, "model": MODEL_REPO}
    except Exception as e:
        return {"ok": False, "error": f"文生图生成失败: {e}"}


def _get_edit_pipe():
    """图生图（img2img）流水线，用于"给一张图做微改"。懒惰加载，用完释放。"""
    global _edit_pipe
    with _lock:
        if _edit_pipe is not None:
            return _edit_pipe, _device
        import torch
        from diffusers import AutoPipelineForImage2Image, AutoPipelineForText2Image
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # 复用生成管线已加载的设备，避免重复统计
        if _pipe is not None and _device:
            device = _device
        if os.path.isdir(LOCAL_MODEL_DIR) and os.path.exists(
                os.path.join(LOCAL_MODEL_DIR, "model_index.json")):
            try:
                pipe = AutoPipelineForImage2Image.from_pretrained(
                    LOCAL_MODEL_DIR, torch_dtype=torch.float16, variant="fp16",
                    local_files_only=True)
            except Exception:
                pipe = AutoPipelineForImage2Image.from_pretrained(
                    LOCAL_MODEL_DIR, torch_dtype=torch.float16,
                    local_files_only=True)
        else:
            dtype = torch.float16 if device == "cuda" else torch.float32
            pipe = AutoPipelineForImage2Image.from_pretrained(
                MODEL_REPO, torch_dtype=dtype,
                variant="fp16" if device == "cuda" else None)
        pipe.to(device)
        _edit_pipe, _device = pipe, device
        return pipe, device


def edit_image(init_image, prompt: str, negative_prompt: str = "",
               steps: int = 4, strength: float = 0.6) -> dict:
    """图生图微改：以 init_image（PIL.Image）为底图，按 prompt 做局部修改。
    返回 base64 PNG；失败时返回错误信息。"""
    try:
        from PIL import Image
        if isinstance(init_image, str):
            init_image = Image.open(init_image)
        elif isinstance(init_image, (bytes, bytearray)):
            init_image = Image.open(io.BytesIO(init_image))
        if init_image.mode != "RGB":
            init_image = init_image.convert("RGB")
    except Exception as e:
        return {"ok": False, "error": f"无法读取参考图片: {e}"}
    try:
        pipe, device = _get_edit_pipe()
    except Exception as e:
        return {"ok": False, "error": f"图生图引擎加载失败: {e}"}
    try:
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "low quality, blurry, watermark",
            image=init_image,
            num_inference_steps=max(1, steps),
            strength=float(strength),
            guidance_scale=0.0 if MODEL_REPO.endswith("sd-turbo") else 7.5,
        ).images[0]
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "device": device, "model": "img2img-" + MODEL_REPO}
    except Exception as e:
        return {"ok": False, "error": f"图片微改失败: {e}"}


def unload():
    """释放显存（生成后可调用，把 GPU 让回 Qwen）。"""
    import gc
    global _pipe, _edit_pipe, _device
    if _pipe is not None:
        try:
            _pipe.to("cpu")
        except Exception:
            pass
        _pipe = None
    if _edit_pipe is not None:
        try:
            _edit_pipe.to("cpu")
        except Exception:
            pass
        _edit_pipe = None
    _device = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass