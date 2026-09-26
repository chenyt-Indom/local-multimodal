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
import urllib.request

MODEL_REPO_DEFAULT = "stabilityai/sd-turbo"


def _cfg_sd(key: str, default: str = "") -> str:
    """从 config 读文生图设置（**配置统一走 config**，环境变量在里面已合并）。"""
    try:
        return str((config.load_config() or {}).get(key) or default).strip()
    except Exception:
        return default


def model_repo() -> str:
    """当前权重名。**每次现取**（不是导入时定死），改配置后不用重启就生效。"""
    return (os.environ.get("SD_MODEL") or _cfg_sd("sd_model")
            or MODEL_REPO_DEFAULT).strip()


# 本地已下载的模型目录（fp16 权重），优先使用，避免联网拉取。
# 打包运行时会自动探测随应用分发的 sd_model 资源目录；源码运行时用配置/环境变量/本机路径。
def _resolve_model_dir() -> str:
    env = os.environ.get("SD_MODEL_DIR") or _cfg_sd("sd_model_dir")
    if env:
        return env
    candidates = [
        config.res("sd_model"),                        # 打包后：_internal/sd_model
        r"E:\local-multimodal-models\sdxl-base",       # 本机下载的 SDXL
        r"D:\local-multimodal-models\sdxl-base",
        r"D:\local-multimodal-models\sd-turbo",        # 旧的 sd-turbo（保底）
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "model_index.json")):
            return c
    return candidates[0]


def local_model_dir() -> str:
    return _resolve_model_dir()


# ---------- 模型代际：决定原生分辨率 / 步数 / 要不要开 CFG ----------
# 2026-09-26 补。原来所有参数都按 sd-turbo（SD2.1 蒸馏版）写死 ——
# 原生 512、4 步、`guidance_scale=0`（CFG 关闭）。换成 SDXL base 后这些全都不对：
# SDXL 原生 1024（喂 512 会明显糊）、要 20~30 步、必须开 CFG。
# 而 guidance=0 正是"提示词画不对内容"的根因 —— 实测：
#   「一只橘猫坐在木质窗台上」→ 河边鹅卵石滩
#   「一位女性站在实验室里微笑」→ 坐在海中央石板上
#   「实训室里工位上的学生」→ 空教室，一个人都没有
# 且 negative_prompt 完全失效（负面词靠的正是 CFG 的反向引导）。
def _name_blob() -> str:
    return (model_repo() + " " + os.path.basename(local_model_dir() or "")).lower()


def is_turbo() -> bool:
    """蒸馏 turbo 系：必须 guidance=0 且只要 1~4 步。"""
    return "turbo" in _name_blob()


def is_xl() -> bool:
    """SDXL 系：原生 1024。"""
    n = _name_blob()
    return ("xl" in n) or ("sdxl" in n)


def native_side() -> int:
    """原生（训练）分辨率边长：SD1.5/SD2.1 = 512，SDXL = 1024。"""
    return 1024 if is_xl() else 512


def base_steps() -> int:
    """一次"正常质量"的步数：turbo 蒸馏只要 4，常规权重 22~28。"""
    if is_turbo():
        return 4
    return 28 if is_xl() else 22


def guidance() -> float:
    """CFG 系数 —— 提示词能不能被"听进去"的关键。
    turbo 蒸馏模型必须 0（开了会过饱和、结构崩），常规权重 7.5。"""
    return 0.0 if is_turbo() else 7.5


# ---------- 工作分辨率：SD 原生只有 512，喂多大的图它就得在多大的画布上重绘 ----------
# ⚠️⚠️ 2026-09-23 修：以前 img2img **不限制底图尺寸** —— 用户拖一张 2400×1600 的照片进来，
# 流水线就真的在 2400×1600 上跑（是原生的 ~14 倍面积）。后果有两个，都很严重：
#   ① 出图**必然歪曲**（模型从没在这个分辨率上训练过，结构全乱）——用户报的"微改歪曲原图"；
#   ② 慢得离谱（实测 44.5 秒/张，正常只要 4~6 秒）。
# ⇒ 底图先缩到长边 ≤ EDIT_MAX_SIDE 再重绘，最后再按**原始尺寸**放回去。
GENERATE_NATIVE = 512          # 生成的原生边长（SD2.1 系）
EDIT_MAX_SIDE = 768            # 微改的工作分辨率上限（长边）
EDIT_MIN_SIDE = 384            # 太小也重绘不出东西，兜个下限
HD_TARGET_MAX = 2048           # hd 交付的最大边长（超分是 4x，4096 太大没必要）


def _fit_working_size(w: int, h: int, max_side: int | None = None) -> tuple:
    """把 (w,h) 折算成"适合交给 SD 重绘"的尺寸：长边 ≤ max_side、保持比例、8 的倍数。

    8 的倍数不是洁癖 —— VAE 下采样 8 倍，尺寸不是 8 的倍数时 diffusers 会**静默裁掉**
    几个像素，微改出来的图和原图对不齐（叠在一起看就是"边缘错位"）。
    """
    # 工作分辨率要贴着模型原生来：SD2.1 是 512，SDXL 是 1024。
    # 在 SDXL 上按 768 重绘等于"喂了张偏小的图"，细节会糊、结构会飘。
    if max_side is None:
        max_side = 1024 if is_xl() else EDIT_MAX_SIDE
    min_side = 512 if is_xl() else EDIT_MIN_SIDE
    w, h = max(1, int(w)), max(1, int(h))
    m = float(max(w, h))
    if m > max_side:
        s = max_side / m
        w, h = int(round(w * s)), int(round(h * s))
    # 长边太小时整体放大到下限以上（否则重绘幅度再好也没细节可依）
    if max(w, h) < min_side:
        s = min_side / float(max(1, max(w, h)))
        w, h = int(round(w * s)), int(round(h * s))
    return max(64, (w // 8) * 8), max(64, (h // 8) * 8)


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

# 用户手动指定的绘图设备："auto" / "cpu" / "gpu"
# 前端右上角的「CPU 模式 / GPU 加速」徽标点一下就能改这个值。
_forced_device = None


def _get_forced() -> str:
    """读取用户选择（缓存首次读到的值，之后以内存为准）。"""
    global _forced_device
    if _forced_device is None:
        try:
            v = (config.load_config().get("t2i_device") or "auto").lower()
        except Exception:
            v = "auto"
        _forced_device = v if v in ("auto", "cpu", "gpu") else "auto"
    return _forced_device


def _pick_device(torch) -> str:
    """决定实际使用的设备。

    强制了 gpu 但显卡不可用时**回落到 cpu**（而不是抛异常让绘图整个不可用）——
    切换接口在切之前已经拒绝过这种情况，所以正常流程不会走到这里；
    这里只是兜底，避免配置被手工改坏后整个功能挂掉。
    """
    forced = _get_forced()
    if forced == "cpu":
        return "cpu"
    if forced == "gpu":
        try:
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _why_no_cuda(torch) -> str:
    """把"为什么用不了显卡"说清楚 —— 用户最需要的就是这句话。"""
    ver = (getattr(torch, "__version__", "") or "")
    if "+cpu" in ver:
        return (f"当前环境装的是 CPU 版 torch（{ver}），它根本没有编译进 CUDA 支持，"
                "任何情况下都用不了显卡。\n"
                "· 用「一键部署.bat」部署的：用最新版脚本重跑一次即可 —— 它会自动挑 CUDA 版镜像"
                "（旧版脚本在有 N 卡的机器上会错拿 CPU 版）。\n"
                "· 手动部署要两件事同时做："
                "① 换用 CUDA 版镜像（--build-arg TORCH_INDEX=…/cu130；"
                "RTX 50 系必须 cu128 以上，cu124 不行）；"
                "② 用 GPU 编排启动，让容器能拿到显卡"
                "（docker compose -f compose.yml -f compose.gpu.yml up -d）。\n"
                "详见使用说明「显卡会自动适配」一节。")
    if getattr(torch, "version", None) and torch.version.cuda is None:
        return f"当前 torch（{ver}）不是 CUDA 编译版本，无法调用显卡。"
    cuda_ver = getattr(getattr(torch, "version", None), "cuda", None)
    try:
        n = int(torch.cuda.device_count())
    except Exception:
        n = 0
    if n == 0:
        return (f"torch 是 CUDA 版（CUDA {cuda_ver}），但检测不到任何显卡设备。"
                "若在 Docker 里运行，多半是容器没有拿到显卡，"
                "需要用 GPU 编排启动：docker compose -f compose.yml -f compose.gpu.yml up -d")
    return "未检测到可用的 CUDA 设备。"


def _probe(device: str) -> tuple:
    """在目标设备上跑一次**真实矩阵运算**，确认它真能算。

    只查 torch.cuda.is_available() 是不够的 —— 那个为 True 但一算就崩
    （显存被占满、驱动版本不匹配、容器透传不完整）的情况相当常见。
    返回 (是否可用, 失败原因)。
    """
    import torch
    try:
        if device == "cuda":
            if not torch.cuda.is_available():
                return False, _why_no_cuda(torch)
            t = torch.randn(128, 128, device="cuda")
            val = float((t @ t).sum().item())
            torch.cuda.synchronize()
            if val != val:          # NaN
                return False, "显卡运算返回了异常结果（NaN），设备可能不稳定"
        else:
            t = torch.randn(128, 128)
            val = float((t @ t).sum().item())
            if val != val:
                return False, "CPU 运算返回了异常结果"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def set_device(mode: str) -> dict:
    """切换绘图设备，**切换前先校验、切换后做真实加载**。

    支持三种选择：
      auto —— 让程序自己判断（默认；有可用显卡就用显卡）
      gpu  —— 强制显卡
      cpu  —— 强制 CPU

    流程：
      1. 在目标设备上跑一次真实运算 —— 不通过就当场拒绝，保持原状；
      2. 记下选择；**只有设备确实变了**才丢弃已加载的流水线并重新加载
         （否则改个 auto↔gpu 这种等价选择，不该白白等十几秒）；
      3. 真把绘图流水线加载到新设备上 —— 这一步才是"确实能跑"的证明。
         模型文件缺失时退化为"仅设备可用"并如实说明。

    返回统一结构，字段含义见 device_info()。
    """
    global _forced_device, _pipe, _edit_pipe, _device

    mode = (mode or "").strip().lower()
    if mode not in ("auto", "cpu", "gpu"):
        cur = device_info()
        return {"ok": False, "requested": mode, "mode": cur["kind"],
                "device": cur["device"], "gpu": cur["gpu"], "torch": cur["torch"],
                "verified": False,
                "reason": f"不支持的模式「{mode}」，只能是 auto / cpu / gpu"}

    import torch
    if mode == "auto":
        target = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        target = "cuda" if mode == "gpu" else "cpu"

    # 目标设备与当前已加载的一致 → 不必重载引擎，直接确认即可。
    # 判断依据只看**实际设备**，不看选择：在「自动」和「GPU」之间来回点、
    # 实际都是 GPU 时，不该白白等十几秒重新加载。
    if _pipe is not None and _device == target:
        try:
            cfg = config.load_config()
            cfg["t2i_device"] = mode
            config.save_config(cfg)
        except Exception:
            pass
        _forced_device = mode
        info = device_info()
        info.update({"ok": True, "requested": mode, "verified": True,
                     "verify_note": f"设备未变化（仍为 {target}），沿用已加载的绘图引擎"})
        return info

    # ---- 第一步：设备自身能不能算 ----
    ok, why = _probe(target)
    if not ok:
        cur = device_info()
        return {"ok": False, "requested": mode, "mode": cur["kind"],
                "device": cur["device"], "gpu": cur["gpu"], "torch": cur["torch"],
                "verified": False, "reason": why}

    # ---- 第二步：记下选择并丢弃旧流水线（设备变了，旧的必须重载）----
    try:
        cfg = config.load_config()
        cfg["t2i_device"] = mode
        config.save_config(cfg)
    except Exception:
        pass      # 落盘失败不影响本次切换，只是重启后会回到上次的值
    _forced_device = mode
    with _lock:
        _pipe = None
        _device = None
        _edit_pipe = None

    # ---- 第三步：真把流水线加载到新设备上 ----
    # 这才是"确实能跑"的证据：设备可用不代表这个模型能装上去
    # （显存不够、权重精度不兼容都会在这步炸）。
    verified, verify_note = True, ""
    model_ready = (os.path.isdir(local_model_dir())
                   and os.path.exists(os.path.join(local_model_dir(), "model_index.json")))
    if model_ready:
        try:
            pipe, dev = _get_pipe()
            if dev != target:
                verified, verify_note = False, (
                    f"流水线实际加载到了 {dev}，与目标的 {target} 不一致")
        except Exception as e:
            _log_exc(f"切换设备后加载绘图引擎（{mode}）", e)
            verified, verify_note = False, f"绘图引擎在 {target} 上加载失败：{e}"
    else:
        verify_note = "绘图模型未安装，本次只校验了设备本身是否可用"

    info = device_info()
    info.update({"ok": verified, "requested": mode, "verified": verified,
                 "verify_note": verify_note})
    if not verified:
        # 加载失败：把选择退回自动，避免之后每次绘图都撞同一个错
        try:
            cfg = config.load_config()
            cfg["t2i_device"] = "auto"
            config.save_config(cfg)
        except Exception:
            pass
        _forced_device = "auto"
        with _lock:
            _pipe = None
            _device = None
            _edit_pipe = None
        info["reason"] = verify_note
        cur = device_info()
        info.update({"mode": cur["kind"], "device": cur["device"], "gpu": cur["gpu"]})
    return info


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


def free_ollama_vram() -> list:
    """画图前让 Ollama 的对话模型**退出显存**，返回被卸载的模型名。

    ⚠️ 2026-09-26 实测（排查"图片怎么这么慢"时挖出来的）：
      12GB 卡上 qwen3-vl:8b 常驻 **5.79GB**（应用把 keep_alive 设成 30 分钟，它就一直赖着），
      而 SDXL 要 **11.5GB** —— 两者**不可能共存**。共存时 SDXL 被挤进共享内存，
      单张图从约 **30 秒变成约 120 秒**（实测 nvidia-smi 只剩 338MB 空闲）。
      代价只是"画完图后下一条消息要重新加载模型"（几秒），远比每张图多等 90 秒划算。
    """
    names = []
    try:
        cfg = config.load_config() or {}
    except Exception:
        cfg = {}
    for key in ("default_model", "code_model", "vision_model", "embed_model"):
        m = str(cfg.get(key) or "").strip()
        if m and m not in names:
            names.append(m)
    if not names:
        return []
    base = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    freed = []
    for m in names:
        try:
            body = json.dumps({"model": m, "keep_alive": 0}).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            freed.append(m)
        except Exception:
            pass                      # Ollama 没开 / 模型没装：忽略，别把画图搞挂
    return freed


def _load_pipe():
    """实际加载流水线（拆出来便于失败后重试）。"""
    free_ollama_vram()                # ★ 先把对话模型请出显存（见该函数说明）
    import torch
    from diffusers import AutoPipelineForText2Image

    # 设备选择：用户在界面上手动切过就听用户的，否则自动探测
    device = _pick_device(torch)
    # 精度必须跟着设备走：CPU 上用 float16 无法推理（diffusers 会直接警告并失败）。
    # 而本机模型目录（ModelScope 下载）常常只含 fp16 权重（unet 尤其），
    # 必须传 variant="fp16" 才能取到文件 —— 所以通用组合是
    # 「fp16 变体取文件 + 按设备精度加载」。
    dtype = torch.float16 if device == "cuda" else torch.float32

    # 优先从本地目录加载（离线，无需联网）
    if os.path.isdir(local_model_dir()) and os.path.exists(
            os.path.join(local_model_dir(), "model_index.json")):
        pipe, last_err = None, None
        for variant in ("fp16", None):
            try:
                kw = {"torch_dtype": dtype, "local_files_only": True}
                if variant:
                    kw["variant"] = variant
                pipe = AutoPipelineForText2Image.from_pretrained(local_model_dir(), **kw)
                break
            except Exception as e:
                last_err = e
        if pipe is None:
            raise last_err or RuntimeError("本地模型加载失败")
    else:
        pipe = AutoPipelineForText2Image.from_pretrained(
            model_repo(), torch_dtype=dtype,
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


def refine_detail(image, prompt: str, negative_prompt: str = "",
                  strength: float = 0.32) -> tuple:
    """**细节精修**（俗称 hires fix）：把图放大一档后用低幅度 img2img 重绘一遍。

    为什么要这一步（2026-09-23 用户报"做工太过粗糙"）：
    以前 `hd=True` 只是拿 RealESRGAN **插值放大** —— 放大不会凭空长出新细节，
    它只会把 512 的软边猜成硬边，看起来就是"糊 + 塑料感 + 油画味"。
    正确做法是**让 SD 自己在更大的画布上重画一遍**（低 strength，只补细节不动构图），
    再交给超分网络。实测这一步能把毛/布料/纹理这类细节真正"长"出来。

    返回 (新图, 说明)；失败时原样返回并说明原因（**绝不让整个生成失败**）。
    """
    w, h = image.size
    if max(w, h) >= 1024:
        return image, "已是高分辨率，跳过精修"
    try:
        big = image.resize((int(w * 2), int(h * 2)), 3)   # 3 = LANCZOS
        pipe, device = _get_edit_pipe()
        try:
            pipe.to(device)
        except Exception:
            pass
        # 总步数要按 strength 折算（见 edit_image 里的说明），
        # 保证实际生效的步数 ≈ base_steps()（turbo 是 4，SDXL 是 28）
        ratio = max(0.05, min(1.0, float(strength)))
        total = max(1, math.ceil(base_steps() / ratio))
        out = pipe(prompt=prompt,
                   negative_prompt=negative_prompt or "low quality, blurry, watermark",
                   image=big, num_inference_steps=total, strength=float(strength),
                   guidance_scale=guidance()).images[0]
        return out, f"细节精修 {w}x{h} → {out.size[0]}x{out.size[1]}"
    except Exception as exc:
        _log_exc("细节精修", exc)
        return image, f"细节精修跳过（{type(exc).__name__}）"


def generate(prompt: str, negative_prompt: str = "", steps: int | None = None,
             width: int = 512, height: int = 512, hd: bool = False) -> dict:
    """文生图，返回 base64 PNG。

    hd=True：**先把画布放大一档做细节精修，再用 RealESRGAN 放大**（见 refine_detail）。
    """
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
            num_inference_steps=max(1, int(steps or base_steps())),
            guidance_scale=guidance(),
            width=width, height=height,
        ).images[0]
        note = ""
        if hd:
            # ① 细节精修（真长细节）→ ② 超分（补像素密度）
            result, extra = refine_detail(result, prompt, negative_prompt)
            note = extra
            # ⚠️⚠️ **超分前必须把 SD 流水线放掉**（2026-09-23 实测）：
            #    两者同时占显存时，RealESRGAN 在 1024 输入上会
            #    `memory allocation failed ... trying to allocate 8.6GB` → 退化到极慢路径，
            #    实测一张 hd 图 **380 秒**；而单独跑只要 18.6 秒。
            #    代价是下一张图要重载 SD（约 10 秒），但 hd 本来就是"我要质量"的请求。
            unload()
            result, up = upscale_image(result)
            note = (note + "；" + up) if note else up
            # 4096 太大也没必要（文件大、界面卡），降到 HD_TARGET_MAX 交付
            if max(result.size) > HD_TARGET_MAX:
                s = HD_TARGET_MAX / float(max(result.size))
                result = result.resize((int(result.size[0] * s), int(result.size[1] * s)), 3)
                note += f" → 交付 {result.size[0]}x{result.size[1]}"
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "device": device, "model": model_repo(),
                "size": f"{result.size[0]}x{result.size[1]}", "hd_note": note}
    except Exception as e:
        return {"ok": False, "error": f"文生图生成失败: {e}"}


def available_upscale() -> bool:
    """本机是否具备超分能力（模型文件存在）。"""
    return ESRGAN_PATH is not None


def device_info() -> dict:
    """当前绘图会用什么设备（不加载模型，仅探测）。

    文生图与图片微改共用同一个 torch 环境，因此两者设备一致。
    返回字段：
      kind/device  —— 实际会用到的（cpu / gpu、cpu / cuda）
      gpu          —— 显卡型号（有的话）
      torch        —— torch 版本
      forced       —— 用户的选择：auto / cpu / gpu
      note         —— 一句话说明现状
      reason       —— **用不了显卡时的具体原因**（前端切换失败要展示它）
      can_gpu      —— 显卡当前是否真的可用
    """
    info = {"device": "cpu", "kind": "cpu", "gpu": None, "torch": None,
            "cuda_available": False, "note": "", "reason": "",
            "forced": "auto", "can_gpu": False}
    try:
        info["forced"] = _get_forced()
    except Exception:
        pass
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda_available"] = bool(torch.cuda.is_available())
        if info["cuda_available"]:
            try:
                info["gpu"] = torch.cuda.get_device_name(0)
            except Exception:
                info["gpu"] = None
            info["can_gpu"] = True
        # 下面反映的是「用户选择 + 实际能力」共同决定的结果。
        # 选了 cpu 就老老实实说 cpu，哪怕机器上有显卡。
        if info["forced"] == "cpu":
            info["device"], info["kind"] = "cpu", "cpu"
            info["note"] = "已锁定为 CPU 模式" + (
                "（本机其实有可用显卡，切换即可加速）" if info["cuda_available"] else "")
        elif info["cuda_available"]:
            info["device"], info["kind"] = "cuda", "gpu"
            info["note"] = "使用显卡加速"
        else:
            info["reason"] = _why_no_cuda(torch)
            info["note"] = info["reason"]
    except Exception as e:
        info["note"] = f"torch 不可用：{e}"
        info["reason"] = info["note"]
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
    free_ollama_vram()                # ★ 同上：SDXL 与 Qwen 在 12GB 卡上不能共存
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
    if os.path.isdir(local_model_dir()) and os.path.exists(
            os.path.join(local_model_dir(), "model_index.json")):
        pipe, last_err = None, None
        for variant in ("fp16", None):
            try:
                kw = {"torch_dtype": dtype, "local_files_only": True}
                if variant:
                    kw["variant"] = variant
                pipe = AutoPipelineForImage2Image.from_pretrained(local_model_dir(), **kw)
                break
            except Exception as e:
                last_err = e
        if pipe is None:
            raise last_err or RuntimeError("本地模型加载失败")
    else:
        pipe = AutoPipelineForImage2Image.from_pretrained(
            model_repo(), torch_dtype=dtype,
            variant="fp16" if device == "cuda" else None)
    pipe.to(device)
    return pipe, device


def _get_edit_pipe():
    """图生图（img2img）流水线，用于"给一张图做微改"。懒惰加载，用完释放。

    与文生图一样加入失败重试与日志（原因见 _get_pipe 说明）。
    """
    # ⚠️ `_device` **必须一起 global**。下面第 519 行会给它赋值，
    # 只写 `global _edit_pipe` 的话 `_device` 就成了**局部变量**，
    # 于是第 515 行的 `return _edit_pipe, _device` 会在赋值前读取它，
    # 抛 `UnboundLocalError: cannot access local variable '_device'` ——
    # 也就是说：**图片微改第一次能用、之后每次必崩**（原文漏了 `_device`）。
    global _edit_pipe, _device
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
    # ⚠️⚠️ **底图必须先归一化到合适的工作分辨率**（2026-09-23 修，这是"微改歪曲原图"的根因）：
    #     原样把 2400×1600 交给 SD，它就在 2400×1600 上重绘 —— 那是原生 512 的 ~14 倍面积，
    #     模型从没在这个尺度上训练过，结构全乱（而且实测 44.5 秒/张）。
    #     这里先缩到长边 ≤768 重绘，完成后**再放回用户原来的尺寸**，
    #     这样用户拿到的图尺寸不变、内容也不再歪。
    orig_size = init_image.size
    work_w, work_h = _fit_working_size(orig_size[0], orig_size[1])
    work = init_image
    if (work_w, work_h) != orig_size:
        work = init_image.resize((work_w, work_h), 3)      # 3 = LANCZOS
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
            image=work,
            num_inference_steps=total_steps,
            strength=float(strength),
            guidance_scale=guidance(),
        ).images[0]
        # 放回用户原来的尺寸（微改不该顺手把人家照片改小）
        back_note = ""
        if result.size != orig_size:
            result = result.resize(orig_size, 3)          # 3 = LANCZOS
            back_note = f"（在 {work_w}x{work_h} 上重绘后还原到 {orig_size[0]}x{orig_size[1]}）"
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                "device": device, "model": "img2img-" + model_repo(),
                "work_size": f"{work_w}x{work_h}", "source_size": f"{orig_size[0]}x{orig_size[1]}",
                "size_note": back_note}
    except Exception as e:
        return {"ok": False, "error": f"图片微改失败: {e}"}


# ---------- 多图缝合（纯 PIL，不经过模型） ----------
COMPOSE_MAX_SIDE = 4096          # 成品长边上限（再大也没人看，还费内存）


def _load_pil(x):
    """把路径 / bytes / data URL / 裸 base64 / PIL.Image 统一成 RGB 的 PIL.Image。"""
    from PIL import Image
    if isinstance(x, Image.Image):
        return x.convert("RGB")
    if isinstance(x, (bytes, bytearray)):
        return Image.open(io.BytesIO(bytes(x))).convert("RGB")
    t = str(x or "").strip()
    if t.startswith("data:") and "," in t:
        t = t.split(",", 1)[1]
    if os.path.isfile(t):
        return Image.open(t).convert("RGB")
    return Image.open(io.BytesIO(base64.b64decode(t, validate=False))).convert("RGB")


def compose(images, layout="horizontal", size=None, gap=0, bg="FFFFFF",
            align="center", cols=None) -> dict:
    """把多张图拼成一张 —— **纯像素拼接、不重绘**，所以接缝是"无缝"的。

    ⚠️ 2026-09-26 新增：用户要「给几张图让它缝合」，而此前只有 edit_image（改单张）。

    layout : horizontal 横排 / vertical 竖排 / grid 网格（默认横排）
    size   : 每张图统一到的**长边**像素（默认按当前底模的原生档：
             横排统一高度、竖排统一宽度、网格统一到 size×size 的框内）
    gap    : 图片之间的间距（像素）；0 = 紧贴，>0 会露出 bg 色的分隔线
    bg     : 间距/补白的颜色（6 位十六进制，带不带 # 都行）
    align  : start / center / end —— 尺寸不齐时怎么对齐（默认居中）
    cols   : grid 时的列数（默认取 √n 向上取整）

    返回 {"ok", "image"(PIL), "size", "cells", "layout", "count"}；
    失败返回 {"ok": False, "error": ...}。
    """
    try:
        from PIL import Image
    except Exception as e:                                    # pragma: no cover
        return {"ok": False, "error": "缺少 Pillow，无法拼接：%s" % e}
    ims = []
    for x in (images or []):
        try:
            ims.append(_load_pil(x))
        except Exception as e:
            return {"ok": False, "error": "有图片读不出来（%s）" % str(e)[:80]}
    if len(ims) < 2:
        return {"ok": False, "error": "至少要两张图片才能缝合（当前 %d 张）" % len(ims)}

    n = len(ims)
    tgt = max(64, int(size or native_side()))
    lay = str(layout or "horizontal").strip().lower()
    lay = {"h": "horizontal", "row": "horizontal", "横排": "horizontal", "横向": "horizontal",
           "v": "vertical", "column": "vertical", "竖排": "vertical", "纵向": "vertical",
           "网格": "grid", "方格": "grid", "拼贴": "grid"}.get(lay, lay)
    if lay not in ("horizontal", "vertical", "grid"):
        lay = "horizontal"

    scaled = []
    if lay == "horizontal":
        # 统一**高度**：一排图的腰线齐，最像"拼在一起"
        for im in ims:
            w, h = im.size
            k = tgt / float(h)
            scaled.append(im.resize((max(1, int(round(w * k))), tgt), Image.LANCZOS))
        ncol, nrow = n, 1
        cw, ch = max(i.width for i in scaled), tgt
    elif lay == "vertical":
        for im in ims:
            w, h = im.size
            k = tgt / float(w)
            scaled.append(im.resize((tgt, max(1, int(round(h * k)))), Image.LANCZOS))
        ncol, nrow = 1, n
        cw, ch = tgt, max(i.height for i in scaled)
    else:
        for im in ims:
            w, h = im.size
            k = tgt / float(max(w, h))
            scaled.append(im.resize((max(1, int(round(w * k))), max(1, int(round(h * k)))),
                                    Image.LANCZOS))
        if cols is None:
            cols = int(math.ceil(math.sqrt(n)))
        cols = max(1, min(int(cols), n))
        ncol = cols
        nrow = int(math.ceil(n / float(cols)))
        cw, ch = max(i.width for i in scaled), max(i.height for i in scaled)

    gap = max(0, int(gap or 0))
    W = ncol * cw + gap * (ncol - 1)
    H = nrow * ch + gap * (nrow - 1)
    # 成品过大就整体缩回去（保持比例）
    if max(W, H) > COMPOSE_MAX_SIDE:
        k = COMPOSE_MAX_SIDE / float(max(W, H))
        W, H = max(1, int(W * k)), max(1, int(H * k))
    # ⚠️ PIL 的 Image.new **不认裸的 "ffffff"**（要么 "#ffffff"、要么 (r,g,b) 元组），
    #    传裸色值会抛 `unknown color specifier` —— 实测踩到，这里统一转成元组。
    color = str(bg or "FFFFFF").strip().lstrip("#").strip() or "FFFFFF"
    try:
        color_rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        color_rgb = (255, 255, 255)
    canvas = Image.new("RGB", (W, H), color_rgb)
    cells = []
    for i, im in enumerate(scaled):
        r, c = divmod(i, ncol)
        x0 = c * (cw + gap)
        y0 = r * (ch + gap)
        if align == "start":
            dx, dy = 0, 0
        elif align == "end":
            dx, dy = cw - im.width, ch - im.height
        else:
            dx, dy = (cw - im.width) // 2, (ch - im.height) // 2
        canvas.paste(im, (x0 + dx, y0 + dy))
        cells.append((x0 + dx, y0 + dy, im.width, im.height))
    if (W, H) != canvas.size:                      # 缩放后按比例折算坐标
        fx, fy = canvas.size[0] / float(W), canvas.size[1] / float(H)
        cells = [(int(x * fx), int(y * fy), int(w * fx), int(h * fy)) for x, y, w, h in cells]
    return {"ok": True, "image": canvas, "size": canvas.size, "cells": cells,
            "layout": lay, "count": n, "gap": gap, "cell": (cw, ch)}


# ---------- 局部重绘（inpainting）：真正"只改这一块" ----------
# ⚠️⚠️ 2026-09-26 加。实测结论摆在前面，免得以后有人又去调 strength 找答案：
#   纯图生图（SDXL base，无蒙版）**做不到"精确改动某个元素"** ——
#     「加眼镜」0.45 / 0.85 → 都没戴上；「换衣服颜色」0.60 / 0.85 → 颜色都没变；
#     「换背景」0.85 两次 → 一次换成别的场景、一次压根没换（只把脸重画了）。
#   原因：img2img 是**整图去噪**，没有"只改这里"的约束。
#   inpainting + 蒙版（白=重绘、黑=保留）才能做到，而且**没被圈到的像素原样保留**。
INPAINT_DEFAULT_REPO = "AI-ModelScope/stable-diffusion-xl-1.0-inpainting-0.1"

# 常用区域预设：给出归一化框 (x0, y0, x1, y1)，0~1，左上为原点。
# 作用：用户说「把衣服换个颜色」时，模型即使拿不到涂抹蒙版，也能用一个**粗略区域**
# 先把"只改这块"做起来（比整图重绘准得多）。
AREA_PRESETS = {
    "whole": (0.0, 0.0, 1.0, 1.0),
    "upper": (0.0, 0.0, 1.0, 0.5), "top": (0.0, 0.0, 1.0, 0.5),
    "lower": (0.0, 0.5, 1.0, 1.0), "bottom": (0.0, 0.5, 1.0, 1.0),
    "left": (0.0, 0.0, 0.5, 1.0), "right": (0.5, 0.0, 1.0, 1.0),
    "center": (0.25, 0.25, 0.75, 0.75), "middle": (0.25, 0.25, 0.75, 0.75),
    "upper-third": (0.0, 0.0, 1.0, 0.34), "middle-third": (0.0, 0.33, 1.0, 0.67),
    "lower-third": (0.0, 0.66, 1.0, 1.0),
    "face": (0.25, 0.08, 0.75, 0.45),          # 人像常见构图的经验值
    # ⚠️ 加眼镜/改表情请用 eyes：实测（2026-09-26）「加眼镜」用 face 会把头发和眼睛
    #    一起重画（眼睛里出现红橙色乱纹），换成**只框眼周**这条紧框后一次就干净了
    #    （黑框眼镜自然架上、镜片后眼睛清晰、人物与背景都不动）。
    "eyes": (0.30, 0.19, 0.70, 0.36),
    "glasses": (0.30, 0.19, 0.70, 0.36),
    "torso": (0.2, 0.4, 0.8, 0.85),            # 人的上半身/衣服
    "background": (0.0, 0.0, 1.0, 1.0),        # 整图（背景类改动靠 prompt 限定）
}


def inpaint_model_dir() -> str:
    """局部重绘模型的本地目录（优先本地，避免联网）。"""
    env = os.environ.get("SD_INPAINT_DIR") or _cfg_sd("inpaint_model_dir")
    if env:
        return env
    candidates = [
        config.res("sd_inpaint"),
        r"E:\local-multimodal-models\sdxl-inpaint",
        r"D:\local-multimodal-models\sdxl-inpaint",
    ]
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "model_index.json")):
            return c
    return candidates[1]


def inpaint_ready() -> bool:
    """局部重绘模型装好了没（装了才能做"精确微改"）。

    ⚠️ 不能只看 `model_index.json` —— 下载是**边下边落盘**的，`model_index.json`
    往往是最先到的几个小文件之一。只看它就"装好了"的话，真正加载权重时会报
    一堆看不懂的缺文件错误（用户看到的只是"微改失败"）。所以这里**必须确认权重真在**：
      · unet 权重（fp16 或完整的 pth 之一）
      · text_encoder / vae 的权重
      · 目录里不能还有 `.incomplete` 残留
    """
    d = inpaint_model_dir()
    if not d or not os.path.isdir(d):
        return False
    if not os.path.exists(os.path.join(d, "model_index.json")):
        return False
    # 还有没下完的分片 → 视为没装好
    for root, _dirs, files in os.walk(d):
        if any(f.endswith(".incomplete") for f in files):
            return False
    need_any = [
        ("unet", ("diffusion_pytorch_model.fp16.safetensors",
                  "diffusion_pytorch_model.safetensors")),
        ("text_encoder", ("model.fp16.safetensors", "model.safetensors",
                          "pytorch_model.fp16.bin", "pytorch_model.bin")),
        ("vae", ("diffusion_pytorch_model.fp16.safetensors",
                 "diffusion_pytorch_model.safetensors")),
    ]
    for sub, names in need_any:
        sd = os.path.join(d, sub)
        if not os.path.isdir(sd):
            return False
        if not any(os.path.exists(os.path.join(sd, n)) for n in names):
            return False
    return True


def mask_from_regions(size, regions, feather: int = 12) -> "object":
    """按归一化矩形列表造蒙版（白=重绘）。regions=[[x0,y0,x1,y1], ...]。

    ⚠️ 边沿做**羽化**（先膨胀再高斯模糊）：不羽化的话重绘区与保留区之间
    会有一条**生硬的接缝**（实测肉眼很明显）。
    """
    from PIL import Image, ImageDraw, ImageFilter
    w, h = size
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    for r in (regions or []):
        try:
            x0, y0, x1, y1 = [float(v) for v in r]
        except Exception:
            continue
        # 支持两种传法：0~1 归一化，或像素坐标
        if max(x0, y0, x1, y1) <= 1.5:
            x0, x1 = x0 * w, x1 * w
            y0, y1 = y0 * h, y1 * h
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        d.rectangle([int(x0), int(y0), int(x1), int(y1)], fill=255)
    if feather > 0:
        m = m.filter(ImageFilter.MaxFilter(3 + 2 * (feather // 8 + 1)))
        m = m.filter(ImageFilter.GaussianBlur(max(1, feather // 3)))
    return m


def mask_from_area(size, area: str, feather: int = 12):
    """按区域名（center/torso/face/lower-third…）造蒙版。认不出就整图。"""
    key = str(area or "").strip().lower()
    box = AREA_PRESETS.get(key)
    if not box:
        return None
    return mask_from_regions(size, [list(box)], feather=feather)


def _fit_mask(mask_img, size):
    """把蒙版对齐到工作分辨率（用 NEAREST，避免插值把边缘糊成一团）。"""
    from PIL import Image
    if mask_img.size != size:
        mask_img = mask_img.resize(size, Image.NEAREST)
    return mask_img.convert("L")


def _load_mask(x, size):
    """把前端涂抹传来的蒙版（base64 / data URL / 路径 / PIL）读成 L 图并对齐尺寸。"""
    from PIL import Image
    if isinstance(x, Image.Image):
        return _fit_mask(x, size)
    if isinstance(x, (bytes, bytearray)):
        return _fit_mask(Image.open(io.BytesIO(bytes(x))).convert("L"), size)
    t = str(x or "").strip()
    if t.startswith("data:") and "," in t:
        t = t.split(",", 1)[1]
    if os.path.isfile(t):
        return _fit_mask(Image.open(t).convert("L"), size)
    return _fit_mask(Image.open(io.BytesIO(base64.b64decode(t, validate=False))).convert("L"), size)


def _load_inpaint_pipe():
    """加载局部重绘流水线（**独立于文生图那份**，两者不会同时用）。"""
    free_ollama_vram()
    import torch
    from diffusers import AutoPipelineForInpainting
    device = _pick_device(torch)
    dtype = torch.float16 if device == "cuda" else torch.float32
    d = inpaint_model_dir()
    if os.path.isdir(d) and os.path.exists(os.path.join(d, "model_index.json")):
        pipe, last_err = None, None
        for variant in ("fp16", None):
            try:
                kw = {"torch_dtype": dtype, "local_files_only": True}
                if variant:
                    kw["variant"] = variant
                pipe = AutoPipelineForInpainting.from_pretrained(d, **kw)
                break
            except Exception as e:
                last_err = e
        if pipe is None:
            raise last_err or RuntimeError("本地重绘模型加载失败")
    else:
        pipe = AutoPipelineForInpainting.from_pretrained(
            str(_cfg_sd("inpaint_model") or INPAINT_DEFAULT_REPO),
            torch_dtype=dtype, variant="fp16" if device == "cuda" else None)
    pipe.to(device)
    return pipe, device


_inpaint_pipe = None


def _get_inpaint_pipe():
    global _inpaint_pipe
    if _inpaint_pipe is None:
        _inpaint_pipe = _load_inpaint_pipe()
    return _inpaint_pipe


def inpaint(init_image, prompt: str, mask=None, regions=None, area: str = "",
            negative_prompt: str = "", strength: float = 0.85,
            steps=None, feather: int = 12) -> dict:
    """**局部重绘**：只重画蒙版圈定的那块，其余像素原样保留。

    mask     : 前端涂抹传来的蒙版（白=要改的地方）；base64 / data URL / 路径 / PIL
    regions  : 归一化矩形列表 [[x0,y0,x1,y1], ...]（0~1），拿不到涂抹蒙版时用它
    area     : 区域预设名（face / torso / lower-third / center / whole …）
    strength : 蒙版内的改动幅度。**inpainting 和 img2img 不是一回事** ——
               这里 0.85 左右才敢说"改得动"，而 1.0 ≈ 完全重画蒙版内区域。
    返回同 edit_image（含 mask_ratio：蒙版占比，用于判断"圈得对不对"）。
    """
    try:
        from PIL import Image
        if not inpaint_ready():
            return {"ok": False,
                    "error": ("还没装「局部重绘」模型（精确微改要用它）。"
                              "目录：%s —— 下载一次即可："
                              "snapshot_download('AI-ModelScope/stable-diffusion-xl-1.0-inpainting-0.1')"
                              % inpaint_model_dir())}
        try:
            src = _load_pil(init_image)
        except Exception as e:
            return {"ok": False, "error": "无法读取参考图片: %s" % e}
        orig_size = src.size
        work_w, work_h = _fit_working_size(orig_size[0], orig_size[1])
        work = src if (work_w, work_h) == orig_size else src.resize((work_w, work_h), 3)
        # 蒙版优先级：涂抹蒙版 > 矩形区域 > 区域预设
        m = None
        if mask is not None:
            try:
                m = _load_mask(mask, (work_w, work_h))
            except Exception as e:
                return {"ok": False, "error": "蒙版读不出来: %s" % e}
        if m is None and regions:
            m = mask_from_regions((work_w, work_h), regions, feather=feather)
        if m is None and str(area or "").strip():
            m = mask_from_area((work_w, work_h), area, feather=feather)
        if m is None:
            return {"ok": False,
                    "error": "要重绘哪一块没说清（既没有涂抹蒙版，也没有 regions / area）"}
        # 统计蒙版占比：太小说明没圈到，太大说明等于整图重绘 —— 都值得提醒
        hist = m.histogram()
        total = float(work_w * work_h) or 1.0
        ratio = sum(i * c for i, c in enumerate(hist)) / (255.0 * total)
        try:
            pipe, device = _get_inpaint_pipe()
        except Exception as e:
            return {"ok": False, "error": "局部重绘引擎加载失败: %s" % e}
        try:
            ratio_s = max(0.05, min(1.0, float(strength)))
            total_steps = max(1, math.ceil(max(1, int(steps or base_steps())) / ratio_s))
            result = pipe(
                prompt=prompt,
                negative_prompt=negative_prompt or "low quality, blurry, watermark, deformed",
                image=work,
                mask_image=m,
                num_inference_steps=total_steps,
                strength=ratio_s,
                guidance_scale=guidance(),
            ).images[0]
            note = ""
            if result.size != orig_size:
                result = result.resize(orig_size, 3)
                note = "（在 %dx%d 上重绘后还原到 %dx%d）" % (
                    work_w, work_h, orig_size[0], orig_size[1])
            buf = io.BytesIO()
            result.save(buf, format="PNG")
            return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
                    "device": device,
                    "model": "inpaint-" + os.path.basename(inpaint_model_dir() or "sdxl-inpaint"),
                    "work_size": "%dx%d" % (work_w, work_h),
                    "mask_ratio": round(ratio, 3), "size_note": note}
        except Exception as e:
            return {"ok": False, "error": "局部重绘失败: %s" % e}
    except Exception as e:            # 兜底，别把请求搞挂
        return {"ok": False, "error": "局部重绘失败: %s" % e}


def unload():
    """释放显存（生成后可调用，把 GPU 让回 Qwen）。"""
    import gc
    # ⚠️ 新增流水线时**必须一起加进这里** —— 忘了的话 Python 会把 `_inpaint_pipe`
    #    当成局部变量，抛 `UnboundLocalError: cannot access local variable`，
    #    而且是在"画完图要释放显存"这条路上炸（本测试就是这么抓到的）。
    global _pipe, _edit_pipe, _inpaint_pipe, _device
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
    if _inpaint_pipe is not None:
        try:
            _inpaint_pipe.to("cpu")
        except Exception:
            pass
        _inpaint_pipe = None
    _device = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass