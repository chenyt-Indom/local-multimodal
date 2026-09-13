# -*- coding: utf-8 -*-
"""文生图引擎（Stable Diffusion 本地，基于 diffusers + torch）。

- 默认加载轻量的 SDXL-Turbo 或 SD-1.5 模型（首次需下载权重，之后完全离线）。
- 与 Qwen3-VL 共用 16G 显存：生成时显存不足会自动退化为 CPU 兜底。
- 生产环境可选配 GPU；本机无 GPU 时退化为纯 CPU（慢但可用）。
完全本地运行，数据不出机。
"""
import base64
import io
import math
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


def _resolve_esrgan_path() -> str | None:
    """定位超分模型（RealESRGAN 4x）。找不到就返回 None，功能自动降级。"""
    env = os.environ.get("ESRGAN_MODEL")
    candidates = [env] if env else []
    candidates += [
        r"D:\training_data\RealESRGAN_x4plus.pth",
        config.res("esrgan", "RealESRGAN_x4plus.pth"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


ESRGAN_PATH = _resolve_esrgan_path()
_device = None
_pipe = None            # 文生图（txt2img）流水线
_edit_pipe = None       # 图生图（img2img 微改）流水线
_upscaler = None        # 超分网络（RealESRGAN）
_lock = threading.Lock()


def _log_exc(where: str, exc: Exception) -> None:
    """把异常完整堆栈写入日志文件（界面只显示简短信息，详情查日志）。"""
    import datetime
    import traceback
    try:
        path = config.data("logs", "t2i_error.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{where}]\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _load_pipe():
    """实际加载流水线（拆出来便于失败后重试）。"""
    import torch
    from diffusers import AutoPipelineForText2Image

    # 本机有无 CUDA：RTX 5070 Ti 应可用
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # 精度必须跟着设备走：CPU 上用 float16 无法推理（diffusers 会直接警告并失败）。
    # 而本机模型目录（ModelScope 下载）常常只含 fp16 权重（unet 尤其），
    # 必须传 variant="fp16" 才能取到文件 —— 所以通用组合是
    # 「fp16 变体取文件 + 按设备精度加载」。
    dtype = torch.float16 if device == "cuda" else torch.float32

    # 优先从本地目录加载（离线，无需联网）
    if os.path.isdir(LOCAL_MODEL_DIR) and os.path.exists(
            os.path.join(LOCAL_MODEL_DIR, "model_index.json")):
        pipe, last_err = None, None
        for variant in ("fp16", None):
            try:
                kw = {"torch_dtype": dtype, "local_files_only": True}
                if variant:
                    kw["variant"] = variant
                pipe = AutoPipelineForText2Image.from_pretrained(LOCAL_MODEL_DIR, **kw)
                break
            except Exception as e:
                last_err = e
        if pipe is None:
            raise last_err or RuntimeError("本地模型加载失败")
    else:
        pipe = AutoPipelineForText2Image.from_pretrained(
            MODEL_REPO, torch_dtype=dtype,
            variant="fp16" if device == "cuda" else None)
    pipe.to(device)
    return pipe, device


def _get_pipe():
    """获取（必要时加载）文生图流水线。

    长时间运行的服务进程偶尔会在加载阶段抛出 `[Errno 22] Invalid argument`，
    重启即可恢复。这里加入**失败自动重试 + 清理显存缓存**，并把完整堆栈写日志，
    避免用户必须重启程序。
    """
    global _pipe, _device
    with _lock:
        if _pipe is not None:
            return _pipe, _device
        last_exc = None
        for attempt in (1, 2):
            try:
                _pipe, _device = _load_pipe()
                return _pipe, _device
            except Exception as exc:
                last_exc = exc
                _log_exc(f"文生图引擎加载（第 {attempt} 次尝试）", exc)
                # 清一次显存缓存再重试（引擎加载失败常与显存/句柄状态有关）
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
        raise last_exc


def _pipe_loaded():
    return _pipe is not None


def generate(prompt: str, negative_prompt: str = "", steps: int = 4,
             width: int = 512, height: int = 512, hd: bool = False) -> dict:
    """文生图，返回 base64 PNG。hd=True 时再用 RealESRGAN 放大到高分辨率。"""
    try:
        pipe, device = _get_pipe()
    except Exception as e:
        return {"ok": False, "error": f"文生图引擎加载失败: {e}"}
    try:
        # 每次生成前把整条流水线统一到目标设备。
        # 否则多次调用后个别组件会漂回 CPU，报
        # "Expected all tensors to be on the same device ... index is on cuda:0,
        #  different from other tensors on cpu"。to() 在同一设备上开销极小。
        try:
            pipe.to(device)
        except Exception:
            pass
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "low quality, blurry, watermark",
            num_inference_steps=max(1, steps),
            guidance_scale=0.0 if MODEL_REPO.endswith("sd-turbo") else 7.5,
            width=width, height=height,
        ).images[0]
        note = ""
        if hd:
            result, note = upscale_image(result)
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "device": device, "model": MODEL_REPO,
                "size": f"{result.size[0]}x{result.size[1]}", "hd_note": note}
    except Exception as e:
        return {"ok": False, "error": f"文生图生成失败: {e}"}


def available_upscale() -> bool:
    """本机是否具备超分能力（模型文件存在）。"""
    return ESRGAN_PATH is not None


def device_info() -> dict:
    """当前绘图会用什么设备（不加载模型，仅探测）。

    文生图与图片微改共用同一个 torch 环境，因此两者设备一致。
    容器默认镜像是 CPU 版 torch → cpu；源码运行若装了 CUDA 版则 → cuda。
    """
    info = {"device": "cpu", "kind": "cpu", "gpu": None, "torch": None,
            "cuda_available": False, "note": ""}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            info["device"] = "cuda"
            info["kind"] = "gpu"
            try:
                info["gpu"] = torch.cuda.get_device_name(0)
            except Exception:
                info["gpu"] = None
        else:
            # 区分「没显卡」和「装了 CPU 版 torch」——后者换镜像即可提速
            if "+cpu" in (torch.__version__ or ""):
                info["note"] = "当前是 CPU 版 torch，有显卡可换 CUDA 版镜像提速"
            else:
                info["note"] = "未检测到可用的 CUDA 设备"
    except Exception as e:
        info["note"] = f"torch 不可用：{e}"
    return info


def _get_upscaler():
    """懒加载超分网络（spandrel 加载 RealESRGAN 权重，避免 basicsr 依赖问题）。"""
    global _upscaler
    with _lock:
        if _upscaler is not None:
            return _upscaler
        if not ESRGAN_PATH:
            return None
        try:
            import spandrel
            import torch
            desc = spandrel.ModelLoader().load_from_file(ESRGAN_PATH)
            net = desc.model.eval()
            device = "cuda" if torch.cuda.is_available() else "cpu"
            net = net.to(device)
            _upscaler = (net, device)
            return _upscaler
        except Exception as exc:
            _log_exc("超分模型加载", exc)
            return None


def upscale_image(image, target_w: int | None = None, target_h: int | None = None):
    """把 PIL 图片用 RealESRGAN 放大（4x），必要时再插值到目标尺寸。

    返回 (新图, 说明文本)；不具备超分能力时原样返回。
    """
    got = _get_upscaler()
    if got is None:
        return image, "未找到超分模型，跳过放大"
    import numpy as np
    import torch
    from PIL import Image

    net, device = got
    src = image.convert("RGB")
    w, h = src.size
    t = torch.from_numpy(np.array(src)).permute(2, 0, 1).unsqueeze(0).float() / 255.0
    t = t.to(device)
    with torch.inference_mode():
        out = net(t)
    arr = out.clamp(0, 1).squeeze(0).permute(1, 2, 0).cpu().numpy()
    arr = (arr * 255).round().astype(np.uint8)
    big = Image.fromarray(arr)

    note = f"{w}x{h} → {big.size[0]}x{big.size[1]}（4x 超分）"
    if target_w and target_h and (big.size[0] < target_w or big.size[1] < target_h):
        big = big.resize((target_w, target_h), Image.LANCZOS)
        note += f" → {target_w}x{target_h}（插值补足）"
    return big, note


def _load_edit_pipe():
    """实际加载 img2img 流水线。"""
    import torch
    from diffusers import AutoPipelineForImage2Image
    device = "cuda" if torch.cuda.is_available() else "cpu"
    # 复用生成管线已加载的设备，避免重复统计
    if _pipe is not None and _device:
        device = _device
    # 与 _load_pipe 同理：精度跟着设备走（CPU 不能用 float16），
    # 同时用 fp16 变体取文件（本机目录往往只有 fp16 权重）。
    dtype = torch.float16 if device == "cuda" else torch.float32
    if os.path.isdir(LOCAL_MODEL_DIR) and os.path.exists(
            os.path.join(LOCAL_MODEL_DIR, "model_index.json")):
        pipe, last_err = None, None
        for variant in ("fp16", None):
            try:
                kw = {"torch_dtype": dtype, "local_files_only": True}
                if variant:
                    kw["variant"] = variant
                pipe = AutoPipelineForImage2Image.from_pretrained(LOCAL_MODEL_DIR, **kw)
                break
            except Exception as e:
                last_err = e
        if pipe is None:
            raise last_err or RuntimeError("本地模型加载失败")
    else:
        pipe = AutoPipelineForImage2Image.from_pretrained(
            MODEL_REPO, torch_dtype=dtype,
            variant="fp16" if device == "cuda" else None)
    pipe.to(device)
    return pipe, device


def _get_edit_pipe():
    """图生图（img2img）流水线，用于"给一张图做微改"。懒惰加载，用完释放。

    与文生图一样加入失败重试与日志（原因见 _get_pipe 说明）。
    """
    global _edit_pipe
    with _lock:
        if _edit_pipe is not None:
            return _edit_pipe, _device
        last_exc = None
        for attempt in (1, 2):
            try:
                _edit_pipe, _device = _load_edit_pipe()
                return _edit_pipe, _device
            except Exception as exc:
                last_exc = exc
                _log_exc(f"图片微改引擎加载（第 {attempt} 次尝试）", exc)
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
        raise last_exc


def edit_image(init_image, prompt: str, negative_prompt: str = "",
               steps: int = 4, strength: float = 0.6) -> dict:
    """图生图微改：以 init_image（PIL.Image）为底图，按 prompt 做局部修改。
    返回 base64 PNG；失败时返回错误信息。"""
    try:
        from PIL import Image
        if isinstance(init_image, str):
            s = init_image.strip()
            if os.path.isfile(s):
                init_image = Image.open(s)
            else:
                # 兼容另外两种常见传法：data URL 与裸 base64
                # （只当路径处理的话，会报 "No such file or directory: 'iVBORw0...'"
                #   这种看不出所以然的错误）
                if s.startswith("data:") and "," in s:
                    s = s.split(",", 1)[1]
                init_image = Image.open(io.BytesIO(base64.b64decode(s, validate=False)))
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
        try:
            pipe.to(device)      # 统一设备，避免多次调用后组件漂回 CPU
        except Exception:
            pass
        # strength 会按比例截掉时间轴前面的步数：真正执行的步数 ≈ 总步数 × strength。
        # SD-Turbo 是少步模型，若总步数直接传 steps(4)，strength 0.6 只剩 2 步，
        # 结果几乎和原图一样（看起来像"没生效"）。所以先把总步数补回来，
        # 确保「实际生效的步数」不低于 steps。
        ratio = max(0.05, min(1.0, float(strength)))
        total_steps = max(1, math.ceil(max(1, steps) / ratio))
        result = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or "low quality, blurry, watermark",
            image=init_image,
            num_inference_steps=total_steps,
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