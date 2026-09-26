# -*- coding: utf-8 -*-
"""抠图（去背景）：rembg + BiRefNet，输出带透明通道的 RGBA PNG。

**为什么用它、而不是自己训或老一套 u2net：**
  · BiRefNet 是当前开源里 SOTA 的二分图像分割（DIS）模型，MIT 许可；
  · 对**发丝 / 毛绒 / 半透明 / 镂空**这类难点边缘的处理，明显好于 u2net 系
    （老模型出来的 alpha 是"块状硬边"，用户一眼就能看出是抠的）；
  · rembg 提供了统一的 ONNX 会话管理 + 模型下载，**CPU 就能跑** ——
    这点很关键：本机那 12GB 显存要留给常驻的对话模型，
    抠图走 CPU 不会跟它抢显存（实测 1024 输入约几秒）。

**模型档位（用 model 参数选）：**
  birefnet-general      默认。通用，绝大多数场景。
  birefnet-portrait     人像专用，头发边缘最好。
  birefnet-massive      大数据集训练，通用质量最高档之一（也最慢）。
  birefnet-general-lite 轻量版，要快要省内存时用。
  isnet-general-use     上一代通用模型，兜底。
  u2net                 最老的一代，只有兼容老流程时才用。
  bria-rmbg             BRIA RMBG-2.0，质量最强 ——
                        ⚠️ **非商业许可**，商用要另外买授权，默认不选它。

**模型存放：** rembg 默认放 `~/.rembg/models/`。本模块会把它改指到项目自己的
模型目录（见 `home()`），这样 Docker 挂载和离线包能整体带走，不依赖用户 home。
"""
import io
import os
import threading
import time

from . import config

# 默认档位：通用 + MIT 许可，质量与兼容性最平衡。
MODEL_DEFAULT = "birefnet-general"
MODEL_PORTRAIT = "birefnet-portrait"

# 允许的档位白名单。给非白名单的值会被拒绝而不是硬塞给 rembg
# —— rembg 收到怪名字会去下载一个不存在的模型，报错很难懂。
MODEL_CHOICES = (
    "birefnet-general",
    "birefnet-portrait",
    "birefnet-massive",
    "birefnet-general-lite",
    "birefnet-hrsod",
    "birefnet-cod",
    "birefnet-dis",
    "isnet-general-use",
    "isnet-anime",
    "u2net",
    "u2net_human_seg",
    "silueta",
    "bria-rmbg",
)

_lock = threading.Lock()
_sessions: dict = {}          # model -> rembg session（会话复用，别每次重建）

# ---------- 模型下载：自己走镜像，不用 rembg 的内置下载 ----------
# ⚠️⚠️ 为什么必须自己下（2026-09-26 实测）：
#   rembg 的下载走 **GitHub 直连**（pooch.retrieve），国内会直接抛
#   `ConnectionResetError: (10054, '远程主机强迫关闭了一个现有的连接')`，
#   用户换个人像档位就卡在那里 —— 表现是"点了没反应/一直转圈"。
#   这里改成**先试镜像、再回退官方**，下完校验 md5，国内也能开箱即用。
#
# 各档位的官方文件名 + md5（从 rembg 的 sessions/*.py 抄来的，随 rembg 升级可能变）。
# 表里没有的档位会交回 rembg 自己处理（不阻断）。
_MODEL_FILES = {
    "birefnet-general": ("BiRefNet-general-epoch_244.onnx",
                         "7a35a0141cbbc80de11d9c9a28f52697"),
    "birefnet-portrait": ("BiRefNet-portrait-epoch_150.onnx",
                          "c3a64a6abf20250d090cd055f12a3b67"),
    "birefnet-massive": ("BiRefNet-massive-TR_DIS5K_TR_TEs-epoch_420.onnx",
                         "33e726a2136a3d59eb0fdf613e31e3e9"),
    "birefnet-general-lite": ("BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
                              "4fab47adc4ff364be1713e97b7e66334"),
    "birefnet-hrsod": ("BiRefNet-HRSOD_DHU-epoch_115.onnx",
                       "c017ade5de8a50ff0fd74d790d268dda"),
    "birefnet-cod": ("BiRefNet-COD-epoch_125.onnx",
                     "f6d0d21ca89d287f17e7afe9f5fd3b45"),
    "birefnet-dis": ("BiRefNet-DIS-epoch_590.onnx",
                     "2d4d44102b446f33a4ebb2e56c051f2b"),
    "u2net": ("u2net.onnx", "60024c5c889badc19c04ad937298a77b"),
    "silueta": ("silueta.onnx", "55e59e0d8062d2f5d013f4725ee84782"),
    "bria-rmbg": ("bria-rmbg-2.0.onnx", None),          # release 未给 md5
}
_RELEASE_BASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
# 加速前缀（实测 gh-proxy.com 最快；空串 = 官方直连兜底）。详见技能 cn-large-file-download。
_MIRROR_PREFIXES = ("https://gh-proxy.com/", "https://ghfast.top/", "")


def _md5_of(path: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _download(url: str, dest: str, timeout: int = 30) -> None:
    """把 url 下到 dest（先写 .part，成功再改名，避免半截文件被当成模型）。"""
    import urllib.request
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(tmp) < 1024 * 1024:
        raise OSError("下载到的文件太小（%d 字节），可能被拦截了" % os.path.getsize(tmp))
    os.replace(tmp, dest)


def ensure_model(model: str) -> tuple:
    """确保某档位的权重已经在本地。返回 (ok, 说明)。

    已就位 → 立刻返回。没就位 → 依次试镜像下载 + md5 校验。
    **不抛异常**（上层要能用它的返回值给用户看得懂的话）。
    """
    name = (model or MODEL_DEFAULT).strip() or MODEL_DEFAULT
    if is_downloaded(name):
        return True, ""

    info = _MODEL_FILES.get(name)
    if not info:
        # 表里没有的档位：交回 rembg 自己下（可能失败，但至少不阻断）
        return True, ""

    fname, md5 = info
    # ⚠️ 落盘名必须是 `<档位名>.onnx` —— rembg 找的是这个（release 上的
    #    `BiRefNet-general-epoch_244.onnx` 那种名字它认不出来，会重新下载）。
    dest_dir = os.path.join(home(), "models", name)
    dest = os.path.join(dest_dir, name + ".onnx")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as exc:
        return False, "建不出模型目录：%s" % exc

    last_err = ""
    with _lock:
        if os.path.isfile(dest):        # 并发时别人已经下好了
            return True, ""
        for prefix in _MIRROR_PREFIXES:
            url = prefix + _RELEASE_BASE + fname
            try:
                _download(url, dest)
            except Exception as exc:
                last_err = "%s → %s" % (prefix or "官方直连", exc)
                continue
            if md5 and _md5_of(dest) != md5:
                last_err = "%s 下到的文件校验不通过" % (prefix or "官方直连")
                try:
                    os.remove(dest)
                except OSError:
                    pass
                continue
            return True, ""
    return False, ("抠图模型 %s 下载失败（已试过国内镜像）。最后一次错误：%s。\n"
                   "可以手动下载放到 %s：\n  %s%s"
                   % (name, last_err, dest_dir, _RELEASE_BASE, fname))



def _resolve_home() -> str:
    """决定 rembg 的模型目录（环境变量 > 项目模型目录 > 用户目录兜底）。"""
    env = os.environ.get("REMBG_HOME")
    if env:
        return env
    candidates = [
        config.res("rembg"),                                  # 打包后：_internal/rembg
        r"E:\本地多模态助手-Docker\models\rembg",              # Docker 挂载目录
        r"D:\local-multimodal-models\rembg",                   # 本机源码运行
        r"E:\local-multimodal-models\rembg",
    ]
    for c in candidates:
        try:
            # 只要父目录在、能建，就用它（模型本体可以还没下载）。
            if os.path.isdir(os.path.dirname(c)) or os.path.isdir(c):
                return c
        except Exception:
            continue
    return candidates[-1]


def home() -> str:
    """模型目录（并保证存在）。"""
    h = _resolve_home()
    try:
        os.makedirs(h, exist_ok=True)
    except Exception:
        # 目录建不出来（比如只读打包目录）→ 退回用户目录，别把功能整挂。
        h = os.path.join(os.path.expanduser("~"), ".rembg")
        os.makedirs(h, exist_ok=True)
    return h


# 必须在 rembg 真正解析模型路径之前把 HOME 指过去。
os.environ.setdefault("REMBG_HOME", home())


def available() -> tuple:
    """依赖是否就绪。返回 (ok, 原因)。不抛异常，方便上层做能力探测。"""
    try:
        import rembg  # noqa: F401
    except Exception as exc:
        return False, "未安装 rembg（pip install rembg）：%s" % exc
    return True, ""


def session(model: str = MODEL_DEFAULT):
    """取（并缓存）一个 rembg 会话。会话里含 ONNX 模型，重建代价高。"""
    name = (model or MODEL_DEFAULT).strip() or MODEL_DEFAULT
    if name not in MODEL_CHOICES:
        raise ValueError("不认识的抠图模型：%s（可选：%s）"
                         % (name, "、".join(MODEL_CHOICES)))
    with _lock:
        s = _sessions.get(name)
        if s is None:
            from rembg import new_session
            s = new_session(name)
            _sessions[name] = s
        return s


def is_downloaded(model: str = MODEL_DEFAULT) -> bool:
    """模型权重是否已经在本地（用来提前提示用户要下载多久）。"""
    h = home()
    name = (model or MODEL_DEFAULT).strip()
    # rembg 2.0.85 的布局：<home>/models/<name>/<name>.onnx
    # 老布局（仍然兼容）：<home>/<name>.onnx
    for p in (os.path.join(h, "models", name, name + ".onnx"),
              os.path.join(h, name + ".onnx")):
        if os.path.isfile(p) and os.path.getsize(p) > 1024 * 1024:
            return True
    return False


def _to_bytes(src) -> bytes:
    """把各种入参统一成图片字节。"""
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    if isinstance(src, str):
        if src.startswith("data:image/"):
            import base64 as _b64
            return _b64.b64decode(src.split(",", 1)[-1])
        if not os.path.isfile(src):
            raise FileNotFoundError("找不到图片：%s" % src)
        with open(src, "rb") as f:
            return f.read()
    # PIL.Image 之类的：交给它自己存成 PNG
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    return buf.getvalue()


def cutout(src, model: str = MODEL_DEFAULT, alpha_matting: bool = False,
           post_process: bool = False):
    """把一张图的主体抠出来，返回带透明通道的 PNG。

    参数：
      src            图片：本地路径 / bytes / data URL / PIL.Image
      model          模型档位，见 MODEL_CHOICES（默认 birefnet-general）
      alpha_matting  **发丝级** alpha 估算。BiRefNet 系自带柔和 alpha，
                     通常**不需要**——只在"边缘形状本身就不对"时才开，
                     因为它更慢、且在干净背景上可能反而不如原生。
      post_process   对 mask 做一次形态学清理（rembg 的 -dc）。
                     老模型（u2net）配它效果提升明显；新模型可以不开。

    返回 dict：{ok, png(bytes), width, height, model, seconds, ...}
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}

    name = (model or MODEL_DEFAULT).strip() or MODEL_DEFAULT
    if name not in MODEL_CHOICES:
        return {"ok": False,
                "error": "不认识的抠图模型：%s（可选：%s）"
                         % (name, "、".join(MODEL_CHOICES))}

    # 权重不在本地时**自己走镜像下**（rembg 内置下载走 GitHub 直连，国内会断）。
    got, why2 = ensure_model(name)
    if not got:
        return {"ok": False, "error": why2}

    try:
        raw = _to_bytes(src)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    t0 = time.time()
    try:
        from rembg import remove
        sess = session(name)
        out = remove(
            raw,
            session=sess,
            alpha_matting=bool(alpha_matting),
            post_process_mask=bool(post_process),
        )
    except Exception as exc:
        return {"ok": False, "error": "抠图失败：%s" % exc}

    if not out:
        return {"ok": False, "error": "抠图没有产出结果"}

    # 读回尺寸，顺便确认确实是 RGBA（有透明通道才算真抠出来了）。
    from PIL import Image
    im = Image.open(io.BytesIO(out))
    w, h = im.size
    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info

    return {
        "ok": True,
        "png": out,
        "width": w,
        "height": h,
        "mode": im.mode,
        "has_alpha": has_alpha,
        "model": name,
        "alpha_matting": bool(alpha_matting),
        "post_process": bool(post_process),
        "seconds": round(time.time() - t0, 1),
    }


def unload(model: str | None = None) -> None:
    """放掉缓存的会话（换模型 / 要腾内存时用）。"""
    with _lock:
        if model is None:
            _sessions.clear()
        else:
            _sessions.pop(model, None)
