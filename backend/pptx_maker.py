# -*- coding: utf-8 -*-
"""PPT 生成：把结构化内容排成 .pptx 文件。

设计原则
--------
1. **模型只填内容，不写代码。** 让 8B 模型即兴写 python-pptx 代码很容易出错，
   而且每次效果都不一样。这里让它只输出「标题 + 要点」，排版交给本模块。
2. **通用，不绑定领域。** 版式是中性的，靠内容决定风格。
3. **每页可单独装饰与微调。** 每页都能带 `layout`（版式）、`decor`（装饰元素）、
   `style`（覆盖强调色/底色/字号/对齐），**改动只影响这一页**。
4. ⚠️ **中文字体必须显式设 `a:ea`（East Asian）**，python-pptx 只设 `font.name`
   管不到中文，WPS/Office 里会掉回宋体甚至方块。

版式（slide["layout"]）
----------------------
content   标题 + 要点（默认）        two_col   左右两栏要点
image_right / image_left  图文左右     image_full 整页图 + 标题条
table     表格                        cards     卡片组
stats     大数字指标                  steps     流程步骤
timeline  时间线                      quote     整页引言
toc       目录                        section   章节过渡页（只用 title）
blank     空白（只放装饰）

装饰（slide["decor"]，可多选）
-----------------------------
page_number 页码   band 侧边色带   corner 角标圆   dots 圆点组   rule 标题下短横线
"""

import os
import re

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

# 16:9
SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)

# 每个主题：封面底色 / 封面文字 / 内页文字 / 强调色 / 内页背景
THEMES = {
    "blue": {"cover_bg": "1F4E79", "cover_fg": "FFFFFF",
             "body": "2B2B2B", "accent": "2E75B6", "bg": "FFFFFF",
             "muted": "6B7A8C", "card": "F2F7FC"},
    "green": {"cover_bg": "2D6A4F", "cover_fg": "FFFFFF",
              "body": "262626", "accent": "40916C", "bg": "FFFFFF",
              "muted": "63796C", "card": "F1F8F4"},
    "warm": {"cover_bg": "B45309", "cover_fg": "FFFFFF",
             "body": "2B2B2B", "accent": "EA8C3A", "bg": "FFFDF8",
             "muted": "8A7256", "card": "FDF3E5"},
    "purple": {"cover_bg": "4C3A8C", "cover_fg": "FFFFFF",
               "body": "2B2B2B", "accent": "7C6BD6", "bg": "FFFFFF",
               "muted": "6E6890", "card": "F4F2FC"},
    "mono": {"cover_bg": "262626", "cover_fg": "FFFFFF",
             "body": "1F1F1F", "accent": "808080", "bg": "FFFFFF",
             "muted": "777777", "card": "F4F4F4"},
    "red": {"cover_bg": "9B2C2C", "cover_fg": "FFFFFF",
            "body": "2B2B2B", "accent": "C53030", "bg": "FFFFFF",
            "muted": "8A6363", "card": "FDF3F3"},
}
DEFAULT_THEME = "blue"

# 可自定义的配色键（**通用词汇**，不是某个生成器的键名）。
# ⚠️ 2026-09-26 加：用户/模型常提「红金色系」「企业蓝」「莫兰迪色」这类要求，
#    而预设只有 6 套 —— 做不到的时候模型就**只好嘴上说做到了**
#    （实测真有：它回「采用党政机关推荐的红金色系」，而当时的代码里根本没有红金）。
#    现在允许直接给十六进制色覆盖，只给一部分也行，其余走预设。
COLOR_KEYS = ("cover_bg", "cover_fg", "body", "accent", "bg", "muted", "card")

# 通用键 → 各生成器主题键的**别名落位**。
# ⚠️ 三个生成器的主题键名并不统一：pptx 用 accent/cover_bg/bg/body/muted/card，
#    而 xlsx 的表头色叫 **head**、隔行底叫 **zebra**，docx 还有 text/quote_bg。
#    少了这层映射就会出现"设了 accent 但表头照样是蓝的"——**静默失效**，
#    2026-09-26 实测踩到：Excel 传 {"accent": "#B8860B"}，表头一变没变。
#    模型只需要记一套通用键，落到哪个生成器由这里分发。
_COLOR_ALIASES = {
    "accent": ("accent", "head", "accent2"),   # 主色：xlsx/docx 的表头与封面标题叫 head，
                                               # docx 的一级列表符号叫 accent2
    "card":   ("card", "zebra"),      # 卡片底 → xlsx 的隔行底纹
    "body":   ("body", "text"),       # 正文色：docx 里叫 text
    "bg":     ("bg", "quote_bg"),     # 浅底 → docx 的引用底色
}

# 这几个键是**从 accent 推导出来**的（不是简单等同）：warm 的 head 比 accent 暗一档、
# docx 的 accent2 比 accent 亮一档，都按**通道比例**映射才不丢那个主题的性格。
_DERIVED_FROM_ACCENT = ("head", "accent2")

# 主色一变，**跟它配套的浅色也要跟着变** —— 它们本来就是主色调浅出来的。
# 不跟着变就会出现"金色表头 + 蓝色隔行底"这种难受的搭配。
# 比例是从预设里量出来的（blue: accent 2E75B6 → zebra EAF1F9 ≈ tint .90、
# line BDD3E8 ≈ tint .68；pptx 的 card F2F7FC ≈ tint .94；docx 的 line D6E4F0 ≈ tint .85）。
# line 取 .80 是折中：xlsx 偏浅一点、docx 几乎吻合，两边都是"能看的浅色分隔线"。
_TINT_OF_ACCENT = {"card": 0.94, "zebra": 0.90, "quote_bg": 0.92,
                   "callout_bg": 0.90, "line": 0.80}


def _chan_scale(new_hex, old_accent, old_head):
    """按预设里 accent→head 的关系，把新主色映射成对应的 head 色。

    预设里两者相等时（blue/green/purple/red、以及 xlsx 全部）直接用新主色；
    不相等时（warm 的 9A4A0B、mono 的 1A1A1A 都是 accent 的加深）按**通道比例**
    缩放，保住那个主题的性格 —— 而不是简单粗暴地等同。
    """
    def _c(h):
        h = str(h or "").lstrip("#")
        try:
            return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        except Exception:
            return None
    a, b, n = _c(old_accent), _c(old_head), _c(new_hex)
    if not a or not b or not n:
        return new_hex
    out = []
    for i in range(3):
        r = (b[i] / a[i]) if a[i] else 1.0
        out.append(max(0, min(255, int(round(n[i] * r)))))
    return "%02X%02X%02X" % tuple(out)


def apply_colors(th: dict, colors) -> dict:
    """把自定义配色覆盖到主题上，返回新字典。

    colors 形如 {"accent": "#B8860B", "cover_bg": "#8B0000"}（键名同 COLOR_KEYS）。
    容错：'#' 可带可不带、大小写随意；非 hex 的值会被忽略并记进 _warn（不抛异常，
    免得一个手滑的颜色把整份文档搞崩）。

    三件事一起做（缺一个都会让"自定义配色"名不副实）：
      ① 主色落到目标主题的 **accent + head** 上；
      ② 与主色配套的浅色（卡片/隔行底/分隔线/引用底）按预设比例**跟着重算**；
      ③ 写回时**沿用目标主题原有的 '#' 风格**（pptx 存 "2E75B6"，xlsx 存 "#2E75B6"）
         —— 否则下游按 "#" 拼接或切片会拿到半截色值。
    """
    out = dict(th or {})
    warns = []
    if not isinstance(colors, dict):
        return out

    def _put(dst, hex6):
        """写进 dst 键，格式跟该键原有取值保持一致；该键不存在就跳过。"""
        if dst not in out:
            return
        out[dst] = ("#" + hex6) if str(out.get(dst) or "").startswith("#") else hex6

    for k, v in colors.items():
        key = str(k or "").strip().lower()
        if key not in COLOR_KEYS:
            continue
        s = str(v or "").strip().lstrip("#").strip()
        if len(s) == 3:                      # #abc → aabbcc
            s = "".join(c * 2 for c in s)
        if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
            warns.append("配色 %s=%r 不是合法的十六进制颜色，已忽略" % (key, v))
            continue
        hex6 = s.upper()
        for dst in _COLOR_ALIASES.get(key, (key,)):
            if dst not in out:
                continue
            if key == "accent" and dst in _DERIVED_FROM_ACCENT and dst != key:
                # head/accent2 是从 accent 派生的（warm 里 head 更暗、docx 里 accent2 更亮），
                # 按预设的通道比例映射；预设里两者相等时（如 xlsx 的 head）比例就是 1，等同主色。
                _put(dst, _chan_scale(hex6, th.get("accent"), th.get(dst)))
            else:
                _put(dst, hex6)

    # 主色被指定了，但没单独给配套浅色 → 按预设比例重算，避免配色割裂
    if "accent" in (colors or {}):
        acc = str(out.get("accent") or th.get("accent") or "2E75B6").lstrip("#")
        for dst, ratio in _TINT_OF_ACCENT.items():
            if dst in out and dst not in (colors or {}):
                _put(dst, _tint(acc, ratio))

    out["_warn"] = (list(th.get("_warn") or []) + warns) if warns else th.get("_warn")
    return out

FONTS = {"yahei": "微软雅黑", "song": "等线", "kai": "楷体", "mono": "等线"}
DEFAULT_FONT = "yahei"

_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
          "right": PP_ALIGN.RIGHT, "justify": PP_ALIGN.JUSTIFY}


# --------------------------------------------------------------------------
# 底层工具
# --------------------------------------------------------------------------
def _rgb(s) -> RGBColor:
    s = str(s or "000000").lstrip("#")
    try:
        return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return RGBColor(0, 0, 0)


def _tint(color, amount=0.85):
    """把颜色往白色方向混合，用做浅底/浅色装饰。amount=0 原色，1 全白。"""
    s = str(color or "000000").lstrip("#")
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        r = g = b = 0
    a = max(0.0, min(1.0, float(amount)))
    return "%02X%02X%02X" % (int(r + (255 - r) * a),
                             int(g + (255 - g) * a),
                             int(b + (255 - b) * a))


def _set_font(run, size, color, bold=False, font=None, italic=False):
    """统一设置字体 —— **中英文都要设**，只设 font.name 管不到中文。

    ⚠️ python-pptx 的 Run 取 XML 元素是 `run._r`（不是 `_element`）。
    """
    f = font or "微软雅黑"
    run.font.size = Pt(size)
    run.font.bold = bool(bold)
    run.font.italic = bool(italic)
    run.font.color.rgb = _rgb(color)
    run.font.name = f
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", f)


def _blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])   # 6 = 完全空白


def _fill_bg(slide, color):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _grad_bg(slide, c1, c2, angle=45):
    """渐变底 —— 封面用，比纯色耐看。"""
    fill = slide.background.fill
    fill.gradient()
    stops = fill.gradient_stops
    stops[0].color.rgb = _rgb(c1)
    stops[0].position = 0.0
    stops[1].color.rgb = _rgb(c2)
    stops[1].position = 1.0
    try:
        fill.gradient_angle = angle
    except Exception:
        pass


def _textbox(slide, x, y, w, h, anchor=None, wrap=True):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    if anchor:
        tf.vertical_anchor = anchor
    return tf


def _rect(slide, x, y, w, h, fill=None, line=None, shape=MSO_SHAPE.RECTANGLE,
          radius=None, shadow=False):
    sp = slide.shapes.add_shape(shape, x, y, w, h)
    if fill:
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(fill)
    else:
        sp.fill.background()
    if line:
        sp.line.color.rgb = _rgb(line)
        sp.line.width = Pt(1)
    else:
        sp.line.fill.background()
    if radius is not None:
        try:
            sp.adjustments[0] = float(radius)
        except Exception:
            pass
    if not shadow:
        try:
            sp.shadow.inherit = False
        except Exception:
            pass
    return sp


def _img_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _add_pic_fit(slide, path, x, y, w, h):
    """按比例把图塞进 (x,y,w,h) 这个框里，居中，不拉伸。"""
    iw, ih = _img_size(path)
    if not iw or not ih:
        return slide.shapes.add_picture(path, x, y, width=w)
    r, R = iw / float(ih), float(w) / float(h)
    if r > R:
        pw, ph = int(w), int(w / r)
    else:
        ph, pw = int(h), int(h * r)
    return slide.shapes.add_picture(path, int(x + (w - pw) / 2),
                                    int(y + (h - ph) / 2), width=pw, height=ph)


def _add_pic_cover(slide, path, x, y, w, h):
    """整页铺满（可超裁一点），用于 image_full 版式。"""
    iw, ih = _img_size(path)
    if not iw or not ih:
        return slide.shapes.add_picture(path, x, y, width=w, height=h)
    r, R = iw / float(ih), float(w) / float(h)
    if r > R:
        ph, pw = int(h), int(h * r)
    else:
        pw, ph = int(w), int(w / r)
    return slide.shapes.add_picture(path, int(x - (pw - w) / 2),
                                    int(y - (ph - h) / 2), width=pw, height=ph)


def _cell_border(cell, edges=("B",), color="D9D9D9", w=9525):
    """给表格单元格加边线。w 单位 EMU（9525 = 0.75pt）。"""
    tcPr = cell._tc.get_or_add_tcPr()
    tag_of = {"L": "a:lnL", "R": "a:lnR", "T": "a:lnT", "B": "a:lnB"}
    for e in edges:
        tag = tag_of.get(e)
        if not tag:
            continue
        for old in tcPr.findall(qn(tag)):
            tcPr.remove(old)
        ln = tcPr.makeelement(qn(tag), {"w": str(w), "cap": "flat",
                                        "cmpd": "sng", "algn": "ctr"})
        fill = ln.makeelement(qn("a:solidFill"), {})
        clr = fill.makeelement(qn("a:srgbClr"), {"val": str(color).lstrip("#")})
        fill.append(clr)
        ln.append(fill)
        # 边线元素必须按 L,R,T,B 的顺序插在 tcPr 靠前的位置
        idx = {"L": 0, "R": 1, "T": 2, "B": 3}[e]
        tcPr.insert(min(idx, len(tcPr)), ln)


def _no_table_style(table):
    """干掉 python-pptx 表格默认的蓝色条纹样式，颜色完全由我们控制。"""
    tbl = table._tbl
    tblPr = tbl.find(qn("a:tblPr"))
    if tblPr is None:
        return
    for el in tblPr.findall(qn("a:tableStyleId")):
        tblPr.remove(el)
    el = tblPr.makeelement(qn("a:tableStyleId"), {})
    el.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"   # No Style, No Grid
    tblPr.append(el)
    tblPr.set("firstRow", "0")
    tblPr.set("bandRow", "0")


# --------------------------------------------------------------------------
# 内容解析
# --------------------------------------------------------------------------
def _bullets_of(items):
    """把要点规整成 [dict(text, level, bold, color, size)]。

    层级靠前导缩进 / 短横线判断；也可以直接给 dict 做单条微调
    （text / level / bold / color / size / italic）。
    """
    out = []
    for it in items or []:
        if isinstance(it, dict):
            txt = str(it.get("text") or "")
            lvl = int(it.get("level") or 0)
            extra = it
        else:
            raw = str(it or "")
            lvl = 1 if re.match(r"^\s{2,}|^\s*[-–—•]\s", raw) else 0
            txt = re.sub(r"^\s{2,}|^\s*[-–—•]\s", "", raw)
            extra = {}
        txt = txt.strip()
        if txt:
            out.append({"text": txt, "level": min(lvl, 1),
                        "bold": extra.get("bold"), "color": extra.get("color"),
                        "size": extra.get("size"), "italic": extra.get("italic"),
                        "mark": extra.get("mark"),
                        "hl": extra.get("hl") if extra.get("hl") is not None
                              else extra.get("highlight")})
    return out


def _fit_size(n, hi=24, mid=21, lo=18, tiny=15):
    return hi if n <= 4 else mid if n <= 6 else lo if n <= 9 else tiny


def _est_block_h(bs, size, w_in):
    """估算一个要点块**实际**会占多高（英寸）。

    ⚠️ 2026-09-25 修：原来是 `n × (size*1.62+9)/72`，比真实排版高估约 1.5 倍
    （真实行距是 1.15 + 段前 9pt）。高估的后果是"明明装得下却被判成放不下"，
    于是整块被顶在最上面、页面下方留一大片白 —— 用户说的"排版空"就是它。
    现在按**下面真正用的排版参数**逐条累加，并估算折行数。
    """
    per_line = max(6.0, w_in * 72.0 / max(6.0, size))     # 一行大约放多少"字宽"
    total = 0.0
    for b in bs:
        lvl = b["level"]
        sz = float(b.get("size") or (size - (2 if lvl else 0)))
        chars = 0.0
        for ch in str(b.get("text") or ""):
            chars += 0.55 if ord(ch) < 0x2E80 else 1.0
        lines = max(1, int(chars / per_line) + (1 if chars % per_line else 0))
        total += lines * sz * 1.15 / 72.0 + (4 if lvl else 9) / 72.0
    return total


# 行内标记：`**粗**` / `==高亮==` / `` `等宽` ``。
# ⚠️ 2026-09-26 加：**模型天然会写 **加粗**** —— 我们自己在 docx 的说明里就这么教它
#    （"正文，可用 **加粗**、==高亮== 标重点"），而 pptx 以前完全不解析，
#    于是幻灯片上直接出现字面的 `**核心任务**`（用户实拍到的现象："文字排版全都没有"）。
#    docx_maker 早就有 _INLINE 做这件事，pptx 这边漏了 —— 现在两边用同一套写法。
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|==.+?==|`[^`]+`)")


def _add_inline(p, text, size, color, font=None, bold=False, italic=False,
                hl=None):
    """把带行内标记的文字铺成多个 run（其余按原样）。

    hl 给了颜色时，`==高亮==` 之外的普通文字**不**加底色；只有 `==…==`
    那几段会加 —— 这样"只标重点"才名副其实。
    """
    for seg in _INLINE_RE.split(str(text or "")):
        if not seg:
            continue
        _b, _mark = bold, None
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            seg, _b = seg[2:-2], True
        elif seg.startswith("==") and seg.endswith("==") and len(seg) > 4:
            seg, _mark = seg[2:-2], (hl or HL_DEFAULT)
        elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
            seg = seg[1:-1]
        r = p.add_run()
        r.text = seg
        _set_font(r, size, color, bold=_b, font=font, italic=italic)
        if _mark:
            _set_hl(r, _hex6(_mark, HL_DEFAULT))
    return p


def _put_bullets(slide, bs, x, y, w, h, th, st, base_size=None, font=None,
                 centered=False, valign="auto"):
    """把要点列表铺进一个文本框。

    valign="auto"：要点少时整块垂直居中（单栏页面不留大片空白）；
    valign="top" ：始终顶对齐（分栏页面各栏要对齐，居中会显得错落）。
    """
    n = len(bs)
    size = float(base_size or _fit_size(n))
    est = _est_block_h(bs, size, w / 914400.0)
    # 装不下就**逐档缩字号**（下限 12pt），绝不截断文字 ——
    # 用户 2026-09-25 明确要求「取消所有篇幅限制」，宁可字小也不能丢内容。
    while est > h and size > 12.0:
        size -= 1.0
        est = _est_block_h(bs, size, w / 914400.0)
    # 阈值 0.99：只要装得下就整块垂直居中（原来 0.92 太严，内容明明不多也顶在上面）
    if valign == "top" or est >= h * 0.99:
        top = y
    else:
        top = y + int((h - Emu(int(est * 914400))) / 2)
    tf = _textbox(slide, x, top, w, min(Emu(int(est * 914400)) + Inches(0.7), h))
    tf.paragraphs[0].text = ""
    first = True
    for b in bs:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        lvl = b["level"]
        p.level = lvl
        p.space_before = Pt(4 if lvl else 9)
        p.line_spacing = 1.15
        if centered:
            p.alignment = PP_ALIGN.CENTER
        sz = float(b.get("size") or (size - (2 if lvl else 0)))
        col = b.get("color") or (st.get("body") or th["body"] if not lvl
                                 else (st.get("muted") or th["muted"]))
        mark = b.get("mark")
        if mark is None:
            mark = "• " if not lvl else "– "
        if mark:
            r0 = p.add_run()
            r0.text = mark
            _set_font(r0, sz, th["accent"] if not lvl else (st.get("muted") or th["muted"]), font=font)
        hl = b.get("hl") if b.get("hl") is not None else b.get("highlight")
        # 走行内解析：**加粗** / ==高亮== / `等宽` 都要真的生效，不能把星号原样印出来
        _add_inline(p, b["text"], sz, col, font=font,
                    bold=bool(b.get("bold")), italic=bool(b.get("italic")),
                    hl=_hex6(hl, HL_DEFAULT) if hl else None)
    return tf


# --------------------------------------------------------------------------
# 装饰
# --------------------------------------------------------------------------
def _decorate(slide, decor, th, st, page_no=None):
    """装饰元素。都在背景层之后加，正文之前调用。"""
    decor = decor or []
    if "band" in decor:
        _rect(slide, Inches(0), Inches(0), Inches(0.22), SLIDE_H,
              fill=st.get("accent") or th["accent"])
    if "corner" in decor:
        c = _rect(slide, SLIDE_W - Inches(2.1), -Inches(1.0), Inches(3.0),
                  Inches(3.0), fill=_tint(st.get("accent") or th["accent"], 0.86),
                  shape=MSO_SHAPE.OVAL)
        el = c._element
        el.getparent().remove(el)
        slide.shapes._spTree.insert(2, el)               # 压到最底层（前两个是画布属性）
    if "dots" in decor:
        for i in range(5):
            _rect(slide, Inches(11.2 + i * 0.28), Inches(6.85), Inches(0.09),
                  Inches(0.09), fill=_tint(th["accent"], 0.35 - i * 0.05),
                  shape=MSO_SHAPE.OVAL)
    if "page_number" in decor and page_no:
        tf = _textbox(slide, SLIDE_W - Inches(1.35), SLIDE_H - Inches(0.62),
                      Inches(0.85), Inches(0.34))
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        r = p.add_run()
        r.text = "%02d" % page_no
        _set_font(r, 11, st.get("muted") or th["muted"], font=st.get("font"))


HL_DEFAULT = "FFE9A8"          # 荧光笔默认色（浅黄，白底黑字也读得清）


def _hex6(v, default=""):
    v = str(v or "").strip().lstrip("#")
    return v if re.match(r"^[0-9A-Fa-f]{6}$", v) else default


def _set_hl(run, color):
    """给这段文字加"荧光笔"高亮 —— 用来标注重点。

    ⚠️ DrawingML 里段落**没有**"底纹"这个概念，高亮是**字符级**的，
    要写进 rPr 的 a:highlight；而且必须按 schema 顺序插在 a:latin 之前，
    顺序错了 PowerPoint 打开会直接判定文件损坏。
    """
    try:
        rPr = run._r.get_or_add_rPr()
        old = rPr.find(qn("a:highlight"))
        if old is not None:
            rPr.remove(old)
        hl = rPr.makeelement(qn("a:highlight"), {})
        hl.append(rPr.makeelement(qn("a:srgbClr"), {"val": color}))
        latin = rPr.find(qn("a:latin"))
        if latin is not None:
            latin.addprevious(hl)
        else:
            rPr.append(hl)
    except Exception:
        pass


def _badge(s, text, th, st):
    """右上角小标签（「重点」「NEW」「必考」）—— 标注整页的性质，位置固定不抢正文。"""
    text = str(text or "").strip()
    if not text:
        return
    # 不截断：标签宽度本来就按字数算（见下面的 w），截了反而丢信息
    w = Inches(0.34 + 0.17 * len(text))
    x = SLIDE_W - w - Inches(0.55)
    y = Inches(0.42)
    if st.get("_logo_path") and str(st.get("logo_pos") or "tr") == "tr":
        y = y + Inches(float(st.get("logo_size") or 0.5) + 0.2)
    _rect(s, x, y, w, Inches(0.36), fill=st.get("accent") or th["accent"],
          shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.4)
    tf = _textbox(s, x, y + Inches(0.035), w, Inches(0.3))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    _set_font(r, 11, "FFFFFF", bold=True, font=st.get("font"))


def _slide_caption(s, text, th, st):
    """页脚小字题注（「图 1 系统架构」「数据来源：……」）—— 放左下，不压正文。"""
    text = str(text or "").strip()
    if not text:
        return
    tf = _textbox(s, Inches(0.85), SLIDE_H - Inches(0.66),
                  Inches(9.2), Inches(0.32))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = text          # 不截断（题注长了自己折行，但不丢字）
    _set_font(r, 10.5, st.get("muted") or th["muted"], font=st.get("font"))


def _page_base(s, th, st, sl, page_no):
    """内页统一底：背景（可用 bg_image）→ 装饰 → 角落 logo。

    ⚠️ 顺序不能反：装饰和 logo 必须压在背景图**上面**，否则直接看不见。
    ⚠️ 用了背景图时，标题/正文默认改成白色 —— 深色字压在照片上读不了。
    """
    path = _image_value(sl or {}, "bg_image", wide=True)
    on_img = bool(path) and _add_bg_image(
        s, path, th, veil=float(st.get("veil") or 0.72),
        color=st.get("veil_color"))
    if on_img:
        # 文字整体提亮，否则深色字压在照片上读不了；二级要点/图注/页码用浅灰
        st["title_color"] = st.get("title_color") or "FFFFFF"
        st["body"] = st.get("body") or "FFFFFF"
        st["muted"] = st.get("muted") or "E4E4E4"
    else:
        _fill_bg(s, st.get("bg") or th["bg"])
    _decorate(s, st.get("decor"), th, st, page_no)
    _add_logo(s, st.get("_logo_path") or "", st.get("logo_pos") or "tr",
              float(st.get("logo_size") or 0.5))
    if sl:                       # 空白页（{}）不画角标和题注
        _badge(s, sl.get("badge"), th, st)
        _slide_caption(s, sl.get("caption"), th, st)


def _title_font(txt, st):
    """按标题长度自动选字号：长标题自动变小，尽量少换行。"""
    base = float(st.get("title_size") or 0)
    if base:
        return base
    n = len(str(txt or ""))
    if n <= 26:
        return 30.0
    if n <= 40:
        return 26.0
    if n <= 64:
        return 22.0
    return 19.0


def _text_lines(txt, size_pt, width_in):
    """粗估文字在给定宽度下占几行（中文按 1 个字宽、其他按 0.55）。

    只需要判断"一行还是两行"，不追求像素级准确。
    """
    if not txt:
        return 1
    per_line = max(4.0, width_in * 72.0 / (size_pt * 0.98))
    total = 0.0
    for ch in str(txt):
        total += 0.55 if ord(ch) < 0x2E80 else 1.0
    return max(1, int(total / per_line) + (1 if total % per_line else 0))


def _title_bar(slide, title, th, st, y=Inches(0.5), rule=True):
    """统一的内页标题：标题 + 下方强调短横线。

    ⚠️ 2026-09-25 改成**自适应**（用户报：标题换行后压在横线上、只有一行时又离正文很远）。
      旧实现把标题框高、字号、横线位置、返回值**全写死**
      （0.9 英寸 / 28pt / y+0.92 / y+1.15）：
        · 标题一换行 → 第二行压到横线和正文上（看起来像排版坏了）；
        · 标题只有一行 → 标题底到正文顶空出 0.7 英寸，正文下面又留一大片白。
      现在：字号按长度自动选 → 按宽度估算行数 → 横线与返回值**都跟着实际行数走**。
      标题也不再 `[:60]` 截断 —— 长标题缩字号换行，一个字都不丢。
    """
    txt = str(title or "").strip() or " "
    size = _title_font(txt, st)
    width_in = 11.7
    lines = _text_lines(txt, size, width_in)
    box_h = Inches(size * 1.2 * lines / 72.0 + 0.14)
    tf = _textbox(slide, Inches(0.85), y, Inches(width_in), box_h)
    p = tf.paragraphs[0]
    p.line_spacing = 1.08
    if st.get("title_align"):
        p.alignment = _ALIGN.get(st["title_align"], PP_ALIGN.LEFT)
    r = p.add_run()
    r.text = txt
    _set_font(r, size, st.get("title_color") or th["body"], bold=True,
              font=st.get("font"))
    if rule:
        rule_y = y + box_h + Inches(0.08)
        _rect(slide, Inches(0.88), rule_y, Inches(1.5), Inches(0.07),
              fill=st.get("accent") or th["accent"])
        return rule_y + Inches(0.30)
    return y + box_h + Inches(0.24)


# --------------------------------------------------------------------------
# 各版式
# --------------------------------------------------------------------------
def _add_cover(prs, th, st, title, subtitle, author, cover_img=""):
    s = _blank(prs)
    if cover_img and os.path.exists(cover_img):
        # 封面用整页图：压一层主题色蒙版，标题才读得清
        _add_bg_image(s, cover_img, th, veil=float(st.get("veil") or 0.66))
    else:
        _grad_bg(s, th["cover_bg"], th["accent"], 45)
    _rect(s, Inches(0.9), Inches(2.05), Inches(1.7), Inches(0.09),
          fill=st.get("accent") or th["cover_fg"])
    tf = _textbox(s, Inches(0.9), Inches(2.4), Inches(11.5), Inches(2.6))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER if st.get("title_align") == "center" else PP_ALIGN.LEFT
    r = p.add_run()
    r.text = title
    _set_font(r, 40 if len(title) <= 18 else 32, th["cover_fg"], bold=True,
              font=st.get("font"))
    if subtitle:
        p2 = tf.add_paragraph()
        p2.alignment = p.alignment
        p2.space_before = Pt(16)
        r2 = p2.add_run()
        r2.text = subtitle
        _set_font(r2, 18, _tint(th["cover_fg"], 0.22), font=st.get("font"))
    if author:
        tf2 = _textbox(s, Inches(0.9), Inches(6.25), Inches(11.5), Inches(0.7))
        p3 = tf2.paragraphs[0]
        p3.alignment = p.alignment
        r3 = p3.add_run()
        r3.text = author
        _set_font(r3, 13, _tint(th["cover_fg"], 0.3), font=st.get("font"))
    return s


def _add_section(prs, th, st, text, index, page_no):
    # 兼容两种调用：直接给标题文字，或把整页配置传进来（这样章节页也能用
    # 角标 / 题注 / 背景图，不用另写一套）
    sl = text if isinstance(text, dict) else {}
    text = str(sl.get("title") or text or "章节")
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    _rect(s, Inches(0), Inches(3.05), Inches(0.28), Inches(1.4),
          fill=st.get("accent") or th["accent"])
    tf = _textbox(s, Inches(0.95), Inches(3.05), Inches(11.4), Inches(1.5))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = "%02d" % index
    _set_font(r, 40, st.get("accent") or th["accent"], bold=True, font=st.get("font"))
    p2 = tf.add_paragraph()
    r2 = p2.add_run()
    r2.text = text
    _set_font(r2, 30, st.get("title_color") or th["body"], bold=True,
              font=st.get("font"))
    return s


def _fallback_content(s, th, st, sl, y=None):
    """版式数据缺失时的回退 —— **画在当前这页上**。

    ⚠️ 以前是 `return _add_content(prs, ...)`，那会**再新建一页**：
    刚建好的那页就变成一张空白页留在文稿里，页码也跟着错位
    （实测 timeline 页因为字段名没对上，直接多出一张重复页）。
    """
    if y is None:
        y = _title_bar(s, sl.get("title"), th, st)
    bs = _bullets_of(sl.get("bullets") or sl.get("items") or [])
    if not bs:
        bs = _bullets_of([
            "（这一页的版式 %s 没给对应内容，已按普通要点页处理）"
            % str(sl.get("layout") or "?")])
    _put_bullets(s, bs, Inches(0.95), y + Inches(0.35), Inches(11.5),
                 SLIDE_H - y - Inches(1.05), th, st,
                 base_size=st.get("body_size"), font=st.get("font"))
    if sl.get("notes"):
        try:
            s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
        except Exception:
            pass
    return s


def _add_content(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st, rule=bool(st.get("rule", True)))
    bs = _bullets_of(sl.get("bullets"))
    _put_bullets(s, bs, Inches(0.95), y + Inches(0.10), Inches(11.45),
                 SLIDE_H - y - Inches(0.75), th, st,
                 base_size=st.get("body_size"), font=st.get("font"),
                 centered=st.get("body_align") == "center")
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_two_col(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    left = sl.get("left") if sl.get("left") is not None else sl.get("bullets")
    right = sl.get("right") or []
    lh = str(sl.get("left_title") or "").strip()
    rh = str(sl.get("right_title") or "").strip()
    colw, gap = Inches(5.55), Inches(0.4)
    # 两栏**共用同一个起点**：先取两栏的高度，再把这一对内容整体垂直居中。
    # 2026-09-25：原来各栏从 y+0.35 顶对齐，3 条要点的页面下面空掉 3 英寸。
    def _est_col(items):
        _bs = _bullets_of(items)
        _sz = float((st.get("body_size") or 0) or _fit_size(len(_bs)))
        return Inches(_est_block_h(_bs, _sz, 5.55) + 0.3)
    _pair_h = min(max(_est_col(left), _est_col(right)), Inches(4.6))
    _content_h = SLIDE_H - y - Inches(0.7)
    _block_top = y + Inches(0.10) + Emu(int(max(0, (_content_h - _pair_h) / 2)))
    for i, (cx, head, items) in enumerate((
            (Inches(0.9), lh, left), (Inches(0.9) + colw + gap, rh, right))):
        top = _block_top
        if head:
            tfh = _textbox(s, cx, top, colw, Inches(0.5))
            ph = tfh.paragraphs[0]
            rh2 = ph.add_run()
            rh2.text = head
            _set_font(rh2, 17, st.get("accent") or th["accent"], bold=True,
                      font=st.get("font"))
            _rect(s, cx, top + Inches(0.48), Inches(0.9), Inches(0.05),
                  fill=_tint(st.get("accent") or th["accent"], 0.45))
            top += Inches(0.75)
        _put_bullets(s, _bullets_of(items), cx, top, colw,
                     SLIDE_H - top - Inches(0.7), th, st,
                     base_size=(st.get("body_size") or 0) or 17, font=st.get("font"),
                     valign="top")
        if i == 0:
            # 竖向分隔线**跟着内容块走**：原来从 y+0.4 起、固定 3.3 英寸高，
            # 内容一居中就对不上（线飘在内容上方）。
            _rect(s, Inches(0.9) + colw + Inches(0.18),
                  _block_top - Inches(0.06), Inches(0.02),
                  Emu(max(int(Inches(0.6)), int(_pair_h))),
                  fill=_tint(th["accent"], 0.75))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_image_side(prs, th, st, sl, page_no, side="right"):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    img_box = (Inches(6.85), y + Inches(0.35), Inches(5.55), SLIDE_H - y - Inches(1.0))
    txt_box = (Inches(0.9), y + Inches(0.35), Inches(5.6), SLIDE_H - y - Inches(1.0))
    if side == "left":
        img_box, txt_box = txt_box, img_box
    path = _slide_image(sl)
    if path and os.path.exists(path):
        _add_pic_fit(s, path, *img_box)
    else:
        _rect(s, img_box[0], img_box[1], img_box[2], img_box[3],
              fill=_tint(st.get("accent") or th["accent"], 0.9),
              shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
        tfp = _textbox(s, img_box[0], img_box[1] + img_box[3] / 2 - Inches(0.2),
                       img_box[2], Inches(0.4))
        pp = tfp.paragraphs[0]
        pp.alignment = PP_ALIGN.CENTER
        rp = pp.add_run()
        rp.text = "（此处配图）"
        _set_font(rp, 14, st.get("muted") or th["muted"], font=st.get("font"))
    _put_bullets(s, _bullets_of(sl.get("bullets")), txt_box[0], txt_box[1],
                 txt_box[2], txt_box[3], th, st,
                 base_size=(st.get("body_size") or 0) or _fit_size(
                     len(sl.get("bullets") or []), 18, 17, 15, 13),
                 font=st.get("font"))
    cap = str(sl.get("image_caption") or "").strip()
    if cap:
        tfc = _textbox(s, img_box[0], SLIDE_H - Inches(0.75), img_box[2], Inches(0.3))
        pc = tfc.paragraphs[0]
        pc.alignment = PP_ALIGN.CENTER
        rc = pc.add_run()
        rc.text = cap
        _set_font(rc, 11, st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_image_full(prs, th, st, sl, page_no):
    s = _blank(prs)
    # 整页图版式想要横向的图，搜索时按横图筛，免得插进来两边留大黑边
    path = _slide_image(sl, wide=True)
    if path and os.path.exists(path):
        _add_pic_cover(s, path, Emu(0), Emu(0), SLIDE_W, SLIDE_H)
        _rect(s, Emu(0), Emu(0), SLIDE_W, Inches(1.55),
              fill=st.get("accent") or th["cover_bg"])
        _rect(s, Emu(0), Inches(1.55), SLIDE_W, Inches(0.06),
              fill=_tint(th["cover_fg"], 0.35))
    else:
        _fill_bg(s, _tint(st.get("accent") or th["accent"], 0.88))
    _add_logo(s, st.get("_logo_path") or "", st.get("logo_pos") or "tr",
              float(st.get("logo_size") or 0.5))
    tf = _textbox(s, Inches(0.85), Inches(0.42), Inches(11.6), Inches(0.75))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = (sl.get("title") or " ")
    _set_font(r, 30, th["cover_fg"], bold=True, font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_table(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    spec = sl.get("table") or {}
    header = [str(x) for x in (spec.get("header") or [])]
    rows = [[str(c) for c in r] for r in (spec.get("rows") or [])]
    if not header and not rows:
        return _fallback_content(s, th, st, sl, y)
    ncol = max([len(header)] + [len(r) for r in rows] or [0])
    nrow = (1 if header else 0) + len(rows)
    acc = st.get("accent") or th["accent"]
    size = float(st.get("body_size") or (16 if ncol <= 4 else 14 if ncol <= 6 else 12))
    avail_h = SLIDE_H - y - Inches(1.0)
    # 行高上限从 0.62 放宽到 0.9：行少的表（3~4 行）原来只有 2.5 英寸高，
    # 顶在上面、下面空一大片（用户说的"做工不够"）。放宽后自然撑起来。
    row_h = min(Inches(0.9), max(Inches(0.42), avail_h // max(1, nrow)))
    # 表格**整体垂直居中**（原来固定 y+0.4 顶对齐）
    est_h = int(row_h) * nrow
    content_h = SLIDE_H - y - Inches(0.85)
    _tbl_top = y + Inches(0.15)
    if est_h < content_h:
        _tbl_top = y + Inches(0.15) + Emu(int((content_h - est_h) / 2))
    gf = s.shapes.add_table(nrow, ncol, Inches(0.9), _tbl_top,
                            Inches(11.5), row_h * nrow)
    tb = gf.table
    _no_table_style(tb)
    # 列宽**按内容长度分配**，不再等分 ——
    # 等分会让「主要任务」这种长文本列挤成三行、而「年度」这种短列空一截。
    # ⚠️ 宽度要**分中英文**算（中文一个字 ≈ 英文 1.8 个字符宽），
    #   否则「预算（万元）」会被判得太窄、表头被迫折成两行（实测踩到）。
    def _w_of(s):
        return sum(0.55 if ord(c) < 0x2E80 else 1.0 for c in str(s))
    _wts = []
    for _ci in range(ncol):
        _h = header[_ci] if _ci < len(header) else ""
        _body = [r[_ci] if _ci < len(r) else "" for r in rows]
        _wts.append(max(_w_of(_h) * 1.25,               # 表头保底：不折行
                        max([_w_of(x) for x in _body] or [1.0]), 4.0))
    _tot = float(sum(_wts)) or 1.0
    for i in range(ncol):
        tb.columns[i].width = int(Inches(11.5) * _wts[i] / _tot)
    ri = 0
    if header:
        for ci in range(ncol):
            c = tb.cell(0, ci)
            c.fill.solid()
            c.fill.fore_color.rgb = _rgb(acc)
            c.margin_left = c.margin_right = Inches(0.1)
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = c.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            # 表头也可能被模型写上行内标记（**加粗** 之类），统一解析，别把标记印出来
            _add_inline(p, header[ci] if ci < len(header) else "", size,
                        "FFFFFF", font=st.get("font"), bold=True)
        ri = 1
    for k, row in enumerate(rows):
        for ci in range(ncol):
            c = tb.cell(ri + k, ci)
            c.fill.solid()
            c.fill.fore_color.rgb = _rgb(th["card"] if k % 2 else "FFFFFF")
            c.margin_left = c.margin_right = Inches(0.1)
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            _cell_border(c, ("B",), _tint(th["accent"], 0.82))
            tf = c.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            _add_inline(p, row[ci] if ci < len(row) else "", size, th["body"],
                        font=st.get("font"))
        tb.rows[ri + k].height = row_h
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


_CHART_KINDS = None


def _chart_kind(name):
    """图表类型名（中英文都认）→ pptx 枚举。"""
    global _CHART_KINDS
    if _CHART_KINDS is None:
        from pptx.enum.chart import XL_CHART_TYPE as T
        _CHART_KINDS = {
            "bar": T.COLUMN_CLUSTERED, "column": T.COLUMN_CLUSTERED,
            "柱状": T.COLUMN_CLUSTERED, "柱状图": T.COLUMN_CLUSTERED,
            "barh": T.BAR_CLUSTERED, "条形": T.BAR_CLUSTERED, "条形图": T.BAR_CLUSTERED,
            "line": T.LINE_MARKERS, "折线": T.LINE_MARKERS, "折线图": T.LINE_MARKERS,
            "pie": T.PIE, "饼图": T.PIE, "饼": T.PIE,
            "doughnut": T.DOUGHNUT, "圆环": T.DOUGHNUT, "环图": T.DOUGHNUT,
            "area": T.AREA, "面积": T.AREA, "面积图": T.AREA,
            "stacked": T.COLUMN_STACKED, "堆叠": T.COLUMN_STACKED,
        }
    return _CHART_KINDS.get(str(name or "bar").strip().lower(),
                            _CHART_KINDS["bar"])


def _is_pie(spec) -> bool:
    return str(spec.get("kind") or "bar").strip().lower() in (
        "pie", "饼图", "饼", "doughnut", "圆环", "环图")


def _add_chart(prs, th, st, sl, page_no):
    """图表页：柱状 / 条形 / 折线 / 饼图 / 圆环 / 面积。

    数据由模型给**结构化数组**（categories + series），不让它画图 ——
    和"模型只填内容、排版交给代码"的原则一致。
    """
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    spec = sl.get("chart") or {}
    cats = [str(c) for c in (spec.get("categories") or [])]
    series = [x for x in (spec.get("series") or []) if isinstance(x, dict)]
    if not cats or not series:
        return _fallback_content(s, th, st, sl, y)

    from pptx.chart.data import CategoryChartData
    data = CategoryChartData()
    data.categories = cats
    for sx in series:
        vals = []
        for v in (sx.get("values") or []):
            try:
                vals.append(float(v))
            except Exception:
                vals.append(0.0)
        data.add_series(str(sx.get("name") or "系列"), vals)

    left, top = Inches(0.9), y + Inches(0.45)
    width = Inches(11.5)
    height = SLIDE_H - top - Inches(0.95)
    try:
        gf = s.shapes.add_chart(_chart_kind(spec.get("kind")), left, top,
                                width, height, data)
    except Exception as e:
        return _fallback_content(s, th, st, sl, y)
    ch = gf.chart
    acc = st.get("accent") or th["accent"]

    if spec.get("title"):
        ch.has_title = True
        try:
            ch.chart_title.text_frame.text = str(spec["title"])
        except Exception:
            pass
    ch.has_legend = bool(spec.get("legend", len(series) > 1))
    try:
        ch.font.size = Pt(float(st.get("body_size") or 12))
        ch.font.name = st.get("font") or _CN_FONT
    except Exception:
        pass

    # ⚠️ 配色默认是 pptx 自带的那套蓝橙，和主题完全撞色 —— 必须手动改。
    # 饼图/圆环按"点"上色，其余按"系列"上色，两条路径不一样。
    palette = [acc, _tint(acc, 0.45), th["body"], th["muted"],
               _tint(acc, 0.7), th["card"]]
    try:
        for plot in ch.plots:
            plot.has_data_labels = bool(spec.get("labels"))
            if plot.has_data_labels:
                try:
                    dl = plot.data_labels
                    dl.font.size = Pt(10)
                    dl.font.color.rgb = _rgb(th["body"])
                except Exception:
                    pass
            for j, ser in enumerate(plot.series):
                try:
                    if _is_pie(spec):
                        for k, pt in enumerate(ser.points):
                            pt.format.fill.solid()
                            pt.format.fill.fore_color.rgb = _rgb(
                                palette[k % len(palette)])
                    else:
                        ser.format.fill.solid()
                        ser.format.fill.fore_color.rgb = _rgb(
                            palette[j % len(palette)])
                except Exception:
                    pass
    except Exception:
        pass

    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_cards(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    cards = [c for c in (sl.get("cards") or []) if isinstance(c, dict)]
    if not cards:
        return _fallback_content(s, th, st, sl, y)
    acc = st.get("accent") or th["accent"]
    n = len(cards)
    gap = Inches(0.32)
    total = Inches(11.6)
    cw = int((total - gap * (n - 1)) / n)
    top = y + Inches(0.62)
    # 卡片高度按可用空间定，但**不超过 3.5"** —— 内容通常只有两三行，
    # 卡片拉太高下半张就是空白（实测导出图片才发现）。
    ch = min(Inches(3.5), SLIDE_H - top - Inches(0.85))
    for i, c in enumerate(cards):
        cx = Inches(0.85) + i * (cw + gap)
        _rect(s, cx, top, cw, ch, fill=st.get("card_bg") or th["card"],
              shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
        _rect(s, cx, top, cw, Inches(0.08), fill=acc)
        # 序号 / 标题 / 正文放进**同一个文本框并整体居中** ——
        # 分成三个框时，内容一旦比框短，下半张卡片就是空白（导出图片才发现）。
        tf = _textbox(s, cx + Inches(0.3), top + Inches(0.3), cw - Inches(0.6),
                      ch - Inches(0.6), anchor=MSO_ANCHOR.MIDDLE)
        pn = tf.paragraphs[0]
        rn = pn.add_run()
        rn.text = str(c.get("index") or ("%02d" % (i + 1)))
        _set_font(rn, 26, _tint(acc, 0.45), bold=True, font=st.get("font"))
        pt = tf.add_paragraph()
        pt.space_before = Pt(6)
        _add_inline(pt, str(c.get("title") or ""), 17, th["body"],
                    font=st.get("font"), bold=True)
        pb = tf.add_paragraph()
        pb.space_before = Pt(8)
        pb.line_spacing = 1.3
        _add_inline(pb, str(c.get("text") or ""), 13,
                    st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_stats(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    items = [x for x in (sl.get("stats") or []) if isinstance(x, dict)]
    if not items:
        return _fallback_content(s, th, st, sl, y)
    acc = st.get("accent") or th["accent"]
    n = len(items)
    gap = Inches(0.4)
    cw = int((Inches(11.6) - gap * (n - 1)) / n)
    for i, it in enumerate(items):
        cx = Inches(0.85) + i * (cw + gap)
        top = y + Inches(1.2)
        tfv = _textbox(s, cx, top, cw, Inches(1.35))
        pv = tfv.paragraphs[0]
        pv.alignment = PP_ALIGN.CENTER
        rv = pv.add_run()
        rv.text = str(it.get("value") or "")
        _set_font(rv, 46 if len(str(it.get("value") or "")) <= 5 else 34,
                  acc, bold=True, font=st.get("font"))
        unit = str(it.get("unit") or "").strip()     # 单位跟着数字走，小一号
        if unit:
            ru = pv.add_run()
            ru.text = unit
            _set_font(ru, 18, acc, bold=True, font=st.get("font"))
        _rect(s, cx + int(cw * 0.32), top + Inches(1.45), int(cw * 0.36),
              Inches(0.05), fill=_tint(acc, 0.5))
        tfl = _textbox(s, cx, top + Inches(1.75), cw, Inches(0.9))
        pl = tfl.paragraphs[0]
        pl.alignment = PP_ALIGN.CENTER
        pl.line_spacing = 1.25
        _add_inline(pl, str(it.get("label") or ""), 14,
                    st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_steps(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    items = [x for x in (sl.get("steps") or []) if isinstance(x, dict)]
    if not items:
        return _fallback_content(s, th, st, sl, y)
    acc = st.get("accent") or th["accent"]
    n = len(items)
    gap = Inches(0.25)
    cw = int((Inches(11.6) - gap * (n - 1)) / n)
    top = y + Inches(0.85)
    card_h = min(Inches(2.95), SLIDE_H - top - Inches(0.85))
    for i, it in enumerate(items):
        cx = Inches(0.85) + i * (cw + gap)
        _rect(s, cx, top, cw, card_h, fill=st.get("card_bg") or th["card"],
              shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
        d = _rect(s, cx + int(cw / 2) - Inches(0.23), top - Inches(0.23),
                  Inches(0.46), Inches(0.46), fill=acc, shape=MSO_SHAPE.OVAL)
        tfd = d.text_frame
        tfd.margin_left = tfd.margin_right = 0
        tfd.margin_top = tfd.margin_bottom = 0
        tfd.vertical_anchor = MSO_ANCHOR.MIDDLE
        pd = tfd.paragraphs[0]
        pd.alignment = PP_ALIGN.CENTER
        rd = pd.add_run()
        rd.text = str(i + 1)
        _set_font(rd, 15, "FFFFFF", bold=True, font=st.get("font"))
        # 标题 + 说明放在同一个文本框里整体居中（同 cards，避免下半张空白）
        tfb = _textbox(s, cx + Inches(0.22), top + Inches(0.42), cw - Inches(0.44),
                       card_h - Inches(0.7), anchor=MSO_ANCHOR.MIDDLE)
        pt = tfb.paragraphs[0]
        pt.alignment = PP_ALIGN.CENTER
        _add_inline(pt, str(it.get("title") or ""), 16, th["body"],
                    font=st.get("font"), bold=True)
        pb = tfb.add_paragraph()
        pb.alignment = PP_ALIGN.CENTER
        pb.space_before = Pt(9)
        pb.line_spacing = 1.3
        _add_inline(pb, str(it.get("text") or ""), 12.5,
                    st.get("muted") or th["muted"], font=st.get("font"))
        if i < n - 1:
            _rect(s, cx + cw + Inches(0.03), top + card_h / 2 - Inches(0.1),
                  Inches(0.19), Inches(0.19), fill=_tint(acc, 0.4),
                  shape=MSO_SHAPE.ISOSCELES_TRIANGLE)
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_timeline(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    # 模型写时间线时，字段名五花八门：items / timeline / steps 都认
    raw = sl.get("items") or sl.get("timeline") or sl.get("steps") or []
    items = [x for x in raw if isinstance(x, dict)]
    if not items:
        return _fallback_content(s, th, st, sl, y)
    acc = st.get("accent") or th["accent"]
    n = len(items)
    axis_y = y + Inches(1.8)
    _rect(s, Inches(1.0), axis_y, Inches(11.3), Inches(0.04),
          fill=_tint(acc, 0.6))
    seg = Inches(11.3) / max(1, n)
    for i, it in enumerate(items):
        cx = Inches(1.0) + int(seg * i + seg / 2)
        _rect(s, cx - Inches(0.11), axis_y - Inches(0.09), Inches(0.22),
              Inches(0.22), fill=acc, shape=MSO_SHAPE.OVAL)
        tfd = _textbox(s, cx - Inches(1.05), axis_y - Inches(0.95), Inches(2.1),
                       Inches(0.45))
        pd = tfd.paragraphs[0]
        pd.alignment = PP_ALIGN.CENTER
        _add_inline(pd, str(it.get("title") or it.get("time")
                            or it.get("label") or ""), 14, acc,
                    font=st.get("font"), bold=True)
        tfb = _textbox(s, cx - Inches(1.0), axis_y + Inches(0.32), Inches(2.0),
                       Inches(1.8))
        pb = tfb.paragraphs[0]
        pb.alignment = PP_ALIGN.CENTER
        pb.line_spacing = 1.3
        _add_inline(pb, str(it.get("text") or ""), 12,
                    st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_quote(prs, th, st, sl, page_no):
    s = _blank(prs)
    acc = st.get("accent") or th["accent"]
    path = _image_value(sl, "bg_image", wide=True) or _slide_image(sl, wide=True)
    if path and os.path.exists(path):
        _add_bg_image(s, path, th, veil=float(st.get("veil") or 0.62))
    else:
        _grad_bg(s, st.get("bg") or th["cover_bg"], acc, 45)
    _add_logo(s, st.get("_logo_path") or "", st.get("logo_pos") or "tr",
              float(st.get("logo_size") or 0.5))
    # ⚠️ 2026-09-25：这一页以前只认 sl["quote"]，模型写 {layout:"quote", text:"..."}
    #    时**正文会被整段丢掉**，页面上只剩 title（实测就是这么翻车的：
    #    模型给了 title+text，渲染出来只有"理念"两个字，引用句一个字都没显示）。
    #    现在三种写法都认：quote 是对象 / quote 是字符串 / 直接给 text。
    q = sl.get("quote")
    if isinstance(q, str):
        q = {"text": q}
    elif not isinstance(q, dict):
        q = {}
    if not q and sl.get("text"):
        q = {"text": sl.get("text")}
    text = str(q.get("text") or sl.get("title") or "").strip()
    src = str(q.get("from") or q.get("author") or "")
    _mark = _textbox(s, Inches(1.3), Inches(1.35), Inches(3.0), Inches(1.4))
    rm = _mark.paragraphs[0].add_run()
    rm.text = "“"
    _set_font(rm, 96, _tint(th["cover_fg"], 0.55), bold=True, font=st.get("font"))
    tf = _textbox(s, Inches(1.9), Inches(2.1), Inches(9.6), Inches(2.9),
                  anchor=MSO_ANCHOR.MIDDLE)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.line_spacing = 1.35
    # 引用句也过一遍行内解析（整句本来就是粗体，这里再解析是为了不让 ** 露出来）
    _add_inline(p, text, 30 if len(text) <= 40 else 24, th["cover_fg"],
                font=st.get("font"), bold=True)
    if src:
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        p2.space_before = Pt(20)
        r2 = p2.add_run()
        r2.text = "—— " + src
        _set_font(r2, 15, _tint(th["cover_fg"], 0.3), font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_toc(prs, th, st, sl, page_no):
    s = _blank(prs)
    st.setdefault("decor", ["band"])
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title") or "目录", th, st)
    items = sl.get("items") or sl.get("bullets") or []
    bs = _bullets_of(items)
    acc = st.get("accent") or th["accent"]
    half = (len(bs) + 1) // 2
    cols = [bs[:half], bs[half:]]
    for ci, col in enumerate(cols):
        if not col:
            continue
        cx = Inches(1.1) + ci * Inches(5.9)
        for i, b in enumerate(col):
            top = y + Inches(0.6) + i * Inches(0.72)
            _rect(s, cx, top + Inches(0.06), Inches(0.42), Inches(0.42),
                  fill=_tint(acc, 0.82), shape=MSO_SHAPE.ROUNDED_RECTANGLE,
                  radius=0.2)
            tfx = _textbox(s, cx, top + Inches(0.05), Inches(0.42), Inches(0.42),
                           anchor=MSO_ANCHOR.MIDDLE)
            px = tfx.paragraphs[0]
            px.alignment = PP_ALIGN.CENTER
            rx = px.add_run()
            rx.text = "%02d" % (ci * half + i + 1)
            _set_font(rx, 13, acc, bold=True, font=st.get("font"))
            tfl = _textbox(s, cx + Inches(0.62), top, Inches(4.6), Inches(0.55),
                           anchor=MSO_ANCHOR.MIDDLE)
            pl = tfl.paragraphs[0]
            rl = pl.add_run()
            rl.text = b["text"]
            _set_font(rl, 16, th["body"], bold=not b["level"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_blank(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    if sl.get("title"):
        _title_bar(s, sl.get("title"), th, st)
    return s


def _add_end(prs, th, st, text):
    s = _blank(prs)
    _grad_bg(s, th["cover_bg"], st.get("accent") or th["accent"], 45)
    _add_logo(s, st.get("_logo_path") or "", st.get("logo_pos") or "tr",
              float(st.get("logo_size") or 0.5))
    tf = _textbox(s, Inches(1.5), Inches(3.15), Inches(10.3), Inches(1.3))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = (text or "谢谢观看")
    _set_font(r, 36, th["cover_fg"], bold=True, font=st.get("font"))
    _rect(s, SLIDE_W / 2 - Inches(0.75), Inches(4.55), Inches(1.5), Inches(0.06),
          fill=th["cover_fg"])
    return s


_LAYOUTS = {
    "content": _add_content, "": _add_content,
    "two_col": _add_two_col, "two-column": _add_two_col, "columns": _add_two_col,
    "image_right": lambda p, t, s, sl, n: _add_image_side(p, t, s, sl, n, "right"),
    "image_left": lambda p, t, s, sl, n: _add_image_side(p, t, s, sl, n, "left"),
    "image_full": _add_image_full, "full_image": _add_image_full,
    "table": _add_table, "cards": _add_cards, "stats": _add_stats,
    "steps": _add_steps, "process": _add_steps,
    "timeline": _add_timeline, "quote": _add_quote, "toc": _add_toc,
    "chart": _add_chart, "charts": _add_chart, "图表": _add_chart,
    "blank": _add_blank,
}


def _resolve(sl, src):
    """把模型写的图片来源变成本地文件。

    统一交给 `img_fetch`：绝对路径 / 网址 / 图片库 id / 图片库名称 / 相对文件名
    都认，网图会自动下载并缓存（细节与坑见 img_fetch 模块说明）。
    """
    from . import img_fetch
    return img_fetch.resolve(src, sl.get("_bases"))


def _image_value(sl, key, wide=False):
    """取某个字段的图：路径 / 网址 / 图库 id 都试，**取不到又像搜索词就联网搜一张**。

    ⚠️ 实测模型经常把搜索词直接写进 `image` / `bg_image`，而不是写在
    `image_query` 里。只按"路径"处理的话它会以为插了图、其实一片空白，还不报错。
    """
    v = str((sl or {}).get(key) or "").strip()
    if not v:
        return ""
    from . import img_fetch
    p = img_fetch.resolve(v, sl.get("_bases"))
    if p:
        return p
    # 解析不到：哪怕它长得像路径，也剥出名字当搜索词试一次
    # （模型会写 "images/植物.jpg" 这种编造路径，直接放弃就一片空白）
    c = img_fetch.as_query(v)
    return img_fetch.search_one(c, want_wide=wide) if c else ""


def _slide_image(sl, key="image", wide=False):
    """取本页配图，**三级兜底**，尽量不让模型"以为插了图、其实一片空白"。：

    ① `image` 字段按路径/网址/图库 id 解析；
    ② 解析不到就当搜索词联网搜（`image_query` 优先，再退到 `image` 本身）；
    ③ 还没有、但这一页确实表达了配图意图 → **用本页标题搜一张**。
    每一次兜底都往告警里记一条，工具会把实情回报给模型。
    """
    from . import img_fetch
    bases = (sl or {}).get("_bases")
    warns = (sl or {}).get("_warn")
    used = (sl or {}).get("_used")       # 同一份文稿里已经用过的图，避免重复
    v = str(sl.get(key) or "").strip()
    q = str(sl.get("image_query") or sl.get("query") or "").strip()

    def _mark(p):
        if p and used is not None:
            used.add(p)
        return p

    if v:
        p = img_fetch.resolve(v, bases)
        if p:
            return _mark(p)

    for cand, why in ((q, "image_query"), (v, key)):
        c = img_fetch.as_query(cand)
        if not c:
            continue
        p = img_fetch.search_one(c, want_wide=wide, exclude=used)
        if p:
            if warns is not None and cand is v:
                warns.append("「%s」不是有效图片，已按搜索词「%s」联网配图"
                             % (str(v)[:30], c))
            return _mark(p)

    title = img_fetch.as_query(sl.get("title"))
    if title and (v or q):
        p = img_fetch.search_one(title, want_wide=wide, exclude=used)
        if p:
            if warns is not None:
                warns.append("「%s」没取到图，已用本页标题「%s」搜图代替"
                             % ((v or q)[:30], title))
            return _mark(p)
    if v and warns is not None:
        warns.append("第「%s」页的图片「%s」没能取到（不是有效路径/网址，"
                     "也没搜到合适的图）" % (str(sl.get("title") or "?")[:16],
                                          str(v)[:30]))
    return ""


def _fill_alpha(shape, alpha):
    """给纯色填充加透明度（0=全透明，1=不透明）。

    python-pptx 没直接暴露这个属性，但底层值就是 `a:srgbClr` 里的 `a:alpha`
    （万分之一为单位）。做"背景图上的蒙版"必须用它，否则文字压在图上读不清。
    """
    try:
        srgb = shape._element.spPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
        for old in srgb.findall(qn("a:alpha")):
            srgb.remove(old)
        el = srgb.makeelement(qn("a:alpha"),
                              {"val": str(int(max(0.0, min(1.0, alpha)) * 100000))})
        srgb.append(el)
    except Exception:
        pass


def _add_bg_image(slide, path, th, veil=0.72, color=None):
    """整页背景图 + 半透明蒙版（保证上面压的文字看得清）。"""
    try:
        _add_pic_cover(slide, path, Emu(0), Emu(0), SLIDE_W, SLIDE_H)
    except Exception:
        return False
    mask = _rect(slide, Emu(0), Emu(0), SLIDE_W, SLIDE_H,
                 fill=color or th["cover_bg"])
    _fill_alpha(mask, veil)
    return True


def _add_logo(slide, path, pos="tr", height_in=0.5, margin_in=0.42):
    """角落小图标/logo（每页统一位置）。"""
    if not path or not os.path.exists(path):
        return
    from . import img_fetch
    iw, ih = img_fetch.size_of(path)
    h = Inches(height_in)
    w = h if not iw or not ih else Emu(int(h * (iw / float(ih))))
    m = Inches(margin_in)
    x = {"tr": SLIDE_W - w - m, "tl": m,
         "br": SLIDE_W - w - m, "bl": m}.get(pos, SLIDE_W - w - m)
    y = m if pos in ("tr", "tl") else SLIDE_H - h - m
    try:
        slide.shapes.add_picture(path, int(x), int(y), width=int(w), height=int(h))
    except Exception:
        pass


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def _page_has_content(page: dict) -> bool:
    """这一页到底有没有东西可画？—— 判定要**把各版式读的字段都算上**。

    ⚠️ 2026-09-26 修（端到端实测发现的）：模型给过一页
    `{"layout": "content"}`（title / bullets 全空）—— 生成出来的成稿里就是
    **一页只有页码 `07` 的白板**。用户拿去汇报很尴尬，而且这种"空页"
    从工具返回的"共 N 页"里完全看不出来（页数是够的）。

    注意不能用 `any(page.values())` 糊弄：`{"layout": "chart"}` 是有值的，
    但画出来还是白板 —— 必须看**真正能渲染出东西**的字段里有没有实质内容。
    """
    if not isinstance(page, dict):
        return False
    for k in ("title", "section", "subtitle", "note"):
        if str(page.get(k) or "").strip():
            return True
    for k in ("bullets", "items", "left", "right", "cards", "stats", "steps",
              "timeline"):
        v = page.get(k)
        if isinstance(v, (list, tuple)) and any(
                (str(x).strip() if not isinstance(x, dict)
                 else any(str(y or "").strip() for y in x.values())) for x in v):
            return True
        if isinstance(v, str) and v.strip():
            return True
    q = page.get("quote")
    if isinstance(q, dict):
        if str(q.get("text") or q.get("content") or "").strip():
            return True
    elif str(q or "").strip():
        return True
    tb = page.get("table")
    if isinstance(tb, dict):
        if tb.get("rows") or tb.get("header") or tb.get("columns"):
            return True
    elif isinstance(tb, (list, tuple)) and tb:
        return True
    ch = page.get("chart")
    if isinstance(ch, dict):
        if any(ch.get(k) for k in ("data", "series", "values", "categories",
                                   "labels", "rows")):
            return True
    elif isinstance(ch, (list, tuple)) and ch:
        return True
    for k in ("image", "image_query", "image_prompt", "bg_image", "cover_image"):
        if str(page.get(k) or "").strip():
            return True
    return False


def build_pptx(path, title, slides, subtitle="", author="", theme=DEFAULT_THEME,
               end_text="", font=DEFAULT_FONT, page_number=True,
               cover=True, end_page=True, img_bases=None,
               logo="", cover_image="", logo_pos="tr", logo_size=0.5,
               colors=None):
    """把结构化内容生成 pptx，返回 {'ok','path','slides','warnings','error'}。

    slides 每项：见模块 docstring。`layout` 决定版式，`decor` 加装饰，
    `style` 做本页微调（accent / bg / card_bg / title_color / title_size /
    title_align / body_size / body_align / font / decor）。
    """
    warnings = []
    try:
        # 先取预设，再用自定义配色覆盖（支持「红金色系」这类要求，见 apply_colors）
        th = apply_colors(
            THEMES.get(str(theme or "").strip().lower(), THEMES[DEFAULT_THEME]),
            colors)
        fname = FONTS.get(str(font or "").strip().lower(), FONTS[DEFAULT_FONT])
        prs = Presentation()
        prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H
        bases = [b for b in (img_bases or []) if b]

        # 全篇共用的图片：logo（每页角落）与封面背景图。解析一次、复用路径。
        from . import img_fetch as _if
        logo_path = _if.resolve(logo, bases) if str(logo or "").strip() else ""

        # ⚠️ 模型常把"封面"单独写成 slides[0]（标题=文档标题、还带个封面配图）。
        # 必须在**画封面之前**识别出来，把它合并进封面配置并跳过 ——
        # 否则会多出一页和大封面重复的空标题页；而"先画了再删掉重画"会留下
        # 重复的 slide XML 部件（实测把 pptx 写坏，解压报 Duplicate name）。
        _used_imgs = set()            # 这份文稿已经用过的配图（防止整篇重复一张）
        _skip_idx = -1
        if cover and slides and isinstance(slides[0], dict):
            _s0 = slides[0]
            _lay0 = str(_s0.get("layout") or "").strip().lower()
            _body0 = any(_s0.get(k) for k in (
                "bullets", "left", "right", "cards", "stats", "steps",
                "table", "chart", "items", "quote"))
            if _lay0 in ("cover", "封面") or (
                    not _body0 and not _s0.get("section")
                    and str(_s0.get("title") or "").strip()
                    == str(title or "").strip()):
                _skip_idx = 0
                if not str(cover_image or "").strip():
                    cover_image = (_s0.get("cover_image") or _s0.get("image_query")
                                   or _s0.get("image") or _s0.get("bg_image") or "")
                subtitle = str(_s0.get("subtitle") or subtitle or "")
                author = str(_s0.get("author") or author or "")

        # 封面背景同样支持"给搜索词"（要横向的图）
        cover_img = _image_value({"_bases": bases, "cover_image": cover_image},
                                 "cover_image", wide=True)

        if cover_img:
            _used_imgs.add(cover_img)
        if cover:
            _add_cover(prs, th, {"font": fname, "veil": None},
                       str(title or "演示文稿"), str(subtitle or ""),
                       str(author or ""), cover_img=cover_img)

        # 版式本身带图片位的（这几类写了 image 会被真的插进去）
        IMG_LAYOUTS = {"image_right", "image_left", "image_full", "full_image",
                       "quote", ""}
        # 放不下配图的版式：写了 image/image_query 只能忽略 —— 但**必须出声**，
        # 否则模型会以为插上了、用户看到一片空白（实测踩到，整份 PPT 零张图）。
        NO_IMG_LAYOUTS = {"two_col", "two-column", "columns", "cards", "stats",
                          "steps", "process", "timeline", "table", "toc",
                          "chart", "section"}

        sec = 0
        made = 0
        for _idx, sl in enumerate(slides or []):
            if _idx == _skip_idx:
                continue
            if not isinstance(sl, dict):
                continue
            # ⚠️ 空页直接跳过 —— 否则成稿里会留下"只有页码的白板页"（实测遇到）。
            #    记进 warnings 让模型知道"你这一页白写了"，它下次会补内容。
            if not _page_has_content(sl):
                warnings.append("第 %d 页没有任何内容（标题/要点/表格/图表都是空的），"
                                "已跳过 —— 空页在成稿里很难看" % (_idx + 1))
                continue
            s = dict(sl)
            s["_bases"] = bases
            s["_warn"] = warnings     # 图片取不到时往这里记（_slide_image 读的是页字典）
            s["_used"] = _used_imgs   # 同一份文稿内已用过的图片路径
            st = dict(s.get("style") or {})
            # ⚠️ 微调字段放在**页级**还是 `style` 里都认 —— 模型很自然会写成
            # {"title":"...", "accent":"C53030"}，只在 style 里找就会静默失效
            # （装饰一个都没出现，还不报错）。两处都收，style 优先。
            for k in ("accent", "bg", "card_bg", "title_color", "title_size",
                      "title_align", "body_size", "body_align", "font",
                      "decor", "rule", "veil", "veil_color", "logo_pos",
                      "logo_size"):
                if s.get(k) is not None and st.get(k) is None:
                    st[k] = s[k]
            st["font"] = st.get("font") or fname
            st["_warn"] = warnings          # 图片取不到时往这里记，回报给模型
            # 页级 logo 优先于全篇 logo（想只给某一页加角标就用它）
            if str(s.get("logo") or "").strip():
                st["_logo_path"] = _if.resolve(s.get("logo"), bases)
            else:
                st["_logo_path"] = logo_path
            st.setdefault("decor", (["page_number"] if page_number else []))
            if not page_number and "page_number" in (st.get("decor") or []):
                st["decor"] = [d for d in st["decor"] if d != "page_number"]
            lay = str(s.get("layout") or "").strip().lower()
            if s.get("section"):
                lay = "section"
            # 兜底：封面配图写在别的地方时，也别让它变成一张普通内容页
            if lay in ("cover", "封面"):
                continue
            if not lay:
                # 没写 layout 时按内容猜：有 table/cards/stats/steps/image 就用对应版式
                for key, name in (("table", "table"), ("cards", "cards"),
                                  ("stats", "stats"), ("steps", "steps"),
                                  ("items", "timeline"), ("quote", "quote")):
                    if s.get(key):
                        lay = name
                        break
                else:
                    if s.get("image"):
                        lay = "image_full" if s.get("image_full") else "image_right"
                    elif s.get("bullets") and not s.get("title"):
                        lay = "blank"
                    else:
                        lay = "content"
            if lay in ("section", "chapter"):
                sec += 1
                made += 1
                _add_section(prs, th, st, s, sec, made + 1)
                continue
            has_img_intent = bool(str(s.get("image") or "").strip()
                                  or str(s.get("image_query") or "").strip())
            if has_img_intent and lay in NO_IMG_LAYOUTS:
                warnings.append(
                    "「%s」这一页写了配图，但版式 %s 放不下图 —— 已忽略。"
                    "要配图请把这页改成 content / image_right / image_full"
                    % (str(s.get("title") or "?")[:14], lay))
            elif has_img_intent and lay in ("content", "") and made >= 0:
                # content 页要配图 → 自动升级成"左文右图"，这是我们最常用的配图版式。
                # 不升级的话，模型写在这儿的 image_query 会被无声丢掉。
                lay = "image_right"
            fn = _LAYOUTS.get(lay)
            if fn is None:
                warnings.append("不认识的版式「%s」，已按普通内容页处理" % lay)
                fn = _add_content
            fn(prs, th, st, s, made + 2)
            made += 1

        if end_page:
            _add_end(prs, th, {"font": fname}, str(end_text or "谢谢观看"))

        out = str(path or "")
        if not out.lower().endswith(".pptx"):
            out += ".pptx"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        prs.save(out)
        return {"ok": True, "path": out, "slides": made + (1 if cover else 0)
                + (1 if end_page else 0), "warnings": warnings, "error": ""}
    except Exception as e:
        return {"ok": False, "path": "", "slides": 0, "warnings": warnings,
                "error": "%s: %s" % (type(e).__name__, e)}


# 兼容旧调用（tools.py 曾用 build_pptx(tmp, title, slides, subtitle=..., ...)）
DEFAULT_THEME_NAME = DEFAULT_THEME
