# -*- coding: utf-8 -*-
"""图片合成：把**主体**贴进**背景**，做出"把 A 图里的人/物放进 B 图"的效果。

与 `t2i.compose()` 的分工（两者名字像，但完全不是一回事）：
  · `t2i.compose()`  —— **排版式拼接**：几张图并排/网格摆一起，各占一格，互不重叠。
  · 本模块 composite —— **内容级合成**：主体坐在背景**上面**，位置/大小/边缘可控。

为什么不是"丢给 SD 重画一张"：那会**改掉主体的长相**（人还是不是那个人全看运气）。
这里的做法是**真实的像素级合成** —— 主体一个像素都不重画，只做位置摆放、
缩放和边缘处理。要"人还是那个人、衣服还是那件衣服"，只能这么做。

主体从哪来：
  · 直接给一张**已抠好**的 PNG（带透明通道）；
  · 或者给原图，`auto_cutout=True` 时自动先抠（走 cutout 模块，BiRefNet）。
"""
import io
import os
import time

# 九宫格锚点 → (横向比例, 纵向比例)，0=贴左/上，1=贴右/下
_POS = {
    "center": (0.5, 0.5),
    "top-left": (0.0, 0.0), "top": (0.5, 0.0), "top-right": (1.0, 0.0),
    "left": (0.0, 0.5), "right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0), "bottom": (0.5, 1.0), "bottom-right": (1.0, 1.0),
}


def _load_image(src, mode="RGBA"):
    """把路径 / bytes / data URL / PIL.Image 统一成 PIL.Image。"""
    from PIL import Image
    if isinstance(src, Image.Image):
        return src.convert(mode)
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(bytes(src))).convert(mode)
    if isinstance(src, str):
        s = src.strip()
        if s.startswith("data:image/"):
            import base64 as _b64
            return Image.open(io.BytesIO(_b64.b64decode(s.split(",", 1)[-1]))).convert(mode)
        if not os.path.isfile(s):
            raise FileNotFoundError("找不到图片：%s" % s)
        return Image.open(s).convert(mode)
    raise TypeError("不支持的图片入参类型：%s" % type(src))


def _hex_rgba(color: str):
    """'RRGGBB' / '#RRGGBB' → (r,g,b,255)。"""
    c = str(color or "FFFFFF").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError("颜色要 6 位十六进制，如 FFFFFF")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)


def _has_alpha(im) -> bool:
    """这张图是不是真的"已经抠好了"（有可用的透明区域）。"""
    if im.mode not in ("RGBA", "LA"):
        return False
    a = im.getchannel("A")
    lo, hi = a.getextrema()
    return lo < 250          # 存在明显透明像素才算


def _feather_alpha(im, radius: float):
    """把 alpha 通道高斯模糊一下 → 边缘变柔（消掉硬切边）。"""
    from PIL import Image, ImageFilter
    if radius <= 0:
        return im
    a = im.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    out = im.copy()
    out.putalpha(a)
    return out


def _shrink_alpha(im, px: int):
    """把 alpha 向内收几像素 —— **去白边/光晕的关键一步**。

    ⚠️⚠️ 为什么必须有（2026-09-26 实测）：抠出来的主体，边缘那一圈像素是
    **半透明的原背景色**（浅色背景下就是发白）。直接贴到深色/复杂背景上，
    人物周围就会浮出一圈白色光晕 —— 一眼假。实测长卷发人像贴海滩时特别明显。

    做法：对 alpha 做**最小值滤波**（腐蚀），让不透明区域整体内缩 `px` 像素，
    把最外圈那层"混了背景色"的像素直接切掉。`px=1~2` 就够，
    给太大（>3）会啃掉发丝和轮廓。
    """
    from PIL import Image, ImageFilter
    px = int(px)
    if px <= 0:
        return im
    size = px * 2 + 1                     # 滤波器尺寸必须是奇数
    a = im.getchannel("A").filter(ImageFilter.MinFilter(size))
    out = im.copy()
    out.putalpha(a)
    return out


def _add_shadow(im, offset=(0, 6), blur=8, opacity=110):
    """给主体加一层柔和投影，让它"坐"在背景上而不是飘着。"""
    from PIL import Image, ImageFilter
    a = im.getchannel("A")
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    tint = Image.new("RGBA", im.size, (0, 0, 0, opacity))
    shadow.paste(tint, offset, a)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    return shadow


def composite(subject, background, position: str = "center", scale: float = 1.0,
              feather: float = 0.0, shadow: bool = False, auto_cutout: bool = True,
              margin: int = 0, canvas: str = "", model: str = "",
              shrink: int = 2):
    """把 subject 合成到 background 上。

    参数：
      subject       主体：抠好的透明 PNG，或原图（auto_cutout 时会自动抠）
      background    背景：图片路径，或 6 位十六进制纯色（如 "FFFFFF"）
      position      摆放位置：center（默认）/ top / bottom / left / right /
                    四角（top-left …），也可直接给 "x,y" 像素坐标
      scale         主体缩放比例（1.0 = 保持原始像素大小）
      feather       边缘羽化像素（0 = 不动；硬切边可给 1~3）
      shadow        是否给主体加柔和投影（贴人物/产品时观感更实）
      auto_cutout   主体没透明通道时，是否自动抠图
      margin        距边缘留白像素（仅对九宫格锚点生效）
      canvas        背景是纯色时，画布尺寸 "WxH"（默认主体放大后的 1.5 倍）
      model         自动抠图用的模型档位（见 cutout.MODEL_CHOICES）
      shrink        **去白边**：把 alpha 向内收几像素（默认 2 —— 实测这个档
                    既把"混了原背景色"的那圈切掉，又不会啃掉发丝）。
                    边缘那圈像素如果不切，浅底抠出的人贴到深背景上会浮出白色光晕。
                    给 0 关闭，给 3 是上限（再多轮廓就受损了）。

    返回 dict：{ok, png(bytes), width, height, cutout(bool), seconds, ...}
    """
    t0 = time.time()
    try:
        from PIL import Image
    except Exception as exc:
        return {"ok": False, "error": "缺少 Pillow：%s" % exc}

    # ---------- 主体 ----------
    try:
        subj = _load_image(subject, "RGBA")
    except Exception as exc:
        return {"ok": False, "error": "主体读取失败：%s" % exc}

    did_cutout = False
    if not _has_alpha(subj):
        if not auto_cutout:
            return {"ok": False,
                    "error": "主体没有透明通道，需要先抠图"
                             "（把 auto_cutout 打开，或先用 cutout_image 抠好）"}
        from . import cutout as _cutout
        r = _cutout.cutout(subject, model=(model or _cutout.MODEL_DEFAULT))
        if not r.get("ok"):
            return {"ok": False, "error": "自动抠图失败：%s" % r.get("error")}
        subj = Image.open(io.BytesIO(r["png"])).convert("RGBA")
        did_cutout = True

    # ---------- 缩放主体 ----------
    try:
        scale = float(scale)
    except Exception:
        scale = 1.0
    scale = max(0.05, min(8.0, scale))
    if abs(scale - 1.0) > 1e-3:
        sw, sh = subj.size
        subj = subj.resize((max(1, int(round(sw * scale))),
                            max(1, int(round(sh * scale)))),
                           Image.LANCZOS)

    # 去白边：**先收 alpha、再羽化**（顺序不能反 —— 反过来会把刚切掉的光晕又糊回来）
    if shrink:
        subj = _shrink_alpha(subj, shrink)
    if feather:
        subj = _feather_alpha(subj, float(feather))

    # ---------- 背景 / 画布 ----------
    is_color = False
    try:
        is_color = not (isinstance(background, (bytes, bytearray))
                        and len(background) > 64)
        if isinstance(background, str):
            s = background.strip()
            is_color = (len(s.lstrip("#")) in (3, 6)
                        and all(ch in "0123456789abcdefABCDEF#" for ch in s))
        else:
            is_color = False
    except Exception:
        is_color = False

    if is_color:
        rgba = _hex_rgba(background)
        if canvas:
            try:
                cw, ch = [int(x) for x in str(canvas).lower().split("x")]
            except Exception:
                return {"ok": False, "error": '画布尺寸要写成 "1024x1024"'}
        else:
            cw = max(int(subj.size[0] * 1.5), 512)
            ch = max(int(subj.size[1] * 1.5), 512)
        bg = Image.new("RGBA", (max(64, cw), max(64, ch)), rgba)
    else:
        try:
            bg = _load_image(background, "RGBA")
        except Exception as exc:
            return {"ok": False, "error": "背景读取失败：%s" % exc}

    canvas_w, canvas_h = bg.size
    subj_w, subj_h = subj.size

    # ---------- 位置 ----------
    pos = str(position or "center").strip().lower()
    if pos in _POS:
        ax, ay = _POS[pos]
        m = max(0, int(margin))
        x = int(round((canvas_w - subj_w) * ax))
        y = int(round((canvas_h - subj_h) * ay))
        # margin：往里收，但不能收成负数位置
        if ax == 0.0:
            x = min(m, max(0, canvas_w - subj_w))
        elif ax == 1.0:
            x = max(0, canvas_w - subj_w - m)
        if ay == 0.0:
            y = min(m, max(0, canvas_h - subj_h))
        elif ay == 1.0:
            y = max(0, canvas_h - subj_h - m)
    else:
        try:
            xs, ys = pos.replace("，", ",").split(",")
            x, y = int(float(xs)), int(float(ys))
        except Exception:
            return {"ok": False,
                    "error": '位置要写九宫格名（center/top-left…）或 "x,y" 像素坐标'}

    # ---------- 合成 ----------
    layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    if shadow:
        layer.alpha_composite(_add_shadow(subj), (x, y))
    layer.alpha_composite(subj, (x, y))
    out = Image.alpha_composite(bg, layer)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    png = buf.getvalue()

    return {
        "ok": True,
        "png": png,
        "width": out.size[0],
        "height": out.size[1],
        "subject_size": (subj_w, subj_h),
        "position": (x, y),
        "cutout": did_cutout,
        "scale": scale,
        "seconds": round(time.time() - t0, 1),
    }
