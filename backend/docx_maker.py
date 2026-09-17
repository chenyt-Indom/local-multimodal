# -*- coding: utf-8 -*-
"""Word 文档排版：把结构化内容排成 .docx 文件。

设计原则
--------
1. **模型只填内容，不写代码**（同 pptx_maker）—— 模型输出「标题 + 段落 + 列表 +
   表格」这类结构化内容，排版交给本模块，稳定性高一个量级。
2. **块模型**：整篇文档 = 一串 block，每个 block 有自己的 type 和可选 style。
   想微调某一块，就在那一块的 style 里写覆盖值，**不影响别处**。
3. ⚠️ **中文字体必须同时设 `w:eastAsia`** —— python-docx 只设 `font.name`
   管不到中文（Word 里会掉回宋体甚至方块）。`_set_style_font()` 统一处理。

行内格式
--------
正文里可以用 `**加粗**`、`*斜体*`、`` `等宽` `` 做局部强调，会被解析成多个 run。

块类型
------
heading(1-4) / para / bullet / number / quote / callout / table / image /
code / divider / pagebreak / toc / end
"""

import os
import re

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# A4
PAGE_W = Cm(21.0)
PAGE_H = Cm(29.7)
TEXT_W = Cm(16.0)          # 正文可用宽度（左右各 2.5cm 页边距）

# 主题：accent 主强调 / accent2 次强调 / text 正文 / muted 次要文字 /
#       quote_bg 引用底 / callout_bg 提示框底 / line 分隔线 / head 标题色
THEMES = {
    "blue": {
        "accent": "1F4E79", "accent2": "2E75B6", "text": "262626",
        "muted": "595959", "quote_bg": "F2F7FC", "callout_bg": "EAF3FB",
        "line": "D6E4F0", "head": "1F4E79",
    },
    "green": {
        "accent": "2D6A4F", "accent2": "40916C", "text": "262626",
        "muted": "595959", "quote_bg": "F1F8F4", "callout_bg": "E7F4EC",
        "line": "D4E9DC", "head": "2D6A4F",
    },
    "warm": {
        "accent": "B45309", "accent2": "EA8C3A", "text": "2B2B2B",
        "muted": "6B5B45", "quote_bg": "FDF6EC", "callout_bg": "FCEFDD",
        "line": "F0DCC0", "head": "9A4A0B",
    },
    "purple": {
        "accent": "4C3A8C", "accent2": "7C6BD6", "text": "2B2B2B",
        "muted": "5F5A78", "quote_bg": "F4F2FC", "callout_bg": "ECE8FA",
        "line": "DCD6F2", "head": "4C3A8C",
    },
    "mono": {
        "accent": "262626", "accent2": "808080", "text": "1F1F1F",
        "muted": "6B6B6B", "quote_bg": "F5F5F5", "callout_bg": "EFEFEF",
        "line": "DDDDDD", "head": "1A1A1A",
    },
    "red": {
        "accent": "9B2C2C", "accent2": "C53030", "text": "2B2B2B",
        "muted": "7A5A5A", "quote_bg": "FDF3F3", "callout_bg": "FBE9E9",
        "line": "F0D5D5", "head": "9B2C2C",
    },
}
DEFAULT_THEME = "blue"

# (正文字体, 标题字体)
FONT_SETS = {
    "yahei": ("微软雅黑", "微软雅黑"),        # 默认：屏幕阅读最舒服
    "song": ("宋体", "黑体"),                 # 正式公文/论文风
    "kai": ("楷体", "黑体"),                  # 偏人文
    "mono": ("等线", "等线"),
}
DEFAULT_FONT = "yahei"

_MONO_CN = "Consolas"
_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


# --------------------------------------------------------------------------
# 底层工具
# --------------------------------------------------------------------------
def _rgb(s):
    s = (str(s or "000000")).lstrip("#")
    try:
        return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return RGBColor(0, 0, 0)


def _shade(el, color):
    """给段落/单元格加底色（w:shd）。el 是 lxml 元素。"""
    if not color:
        return
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), str(color).lstrip("#"))
    el.append(shd)


def _set_font(run, size, color, bold=False, font=None, italic=False,
              mono=False, underline=False):
    """统一设置 run 的字体 —— **中英文都要设**，只设 font.name 管不到中文。"""
    f = _MONO_CN if mono else (font or "微软雅黑")
    run.font.size = Pt(size)
    run.font.bold = bool(bold)
    run.font.italic = bool(italic)
    run.font.underline = bool(underline)
    run.font.color.rgb = _rgb(color)
    run.font.name = f
    rPr = run._r.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.insert(0, rf)
    rf.set(qn("w:ascii"), f)
    rf.set(qn("w:hAnsi"), f)
    rf.set(qn("w:eastAsia"), f)     # ← 关键：中文走这个
    rf.set(qn("w:cs"), f)


def _style_font(style, font):
    """把默认样式（Normal / Heading N）的中英文字体一起改掉。"""
    style.font.name = font
    rPr = style.element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.insert(0, rf)
    for k in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rf.set(qn(k), font)


def _para_border(p, side, color, size=18, space=6):
    """给段落加单边边框（用做引用块的左侧色条）。"""
    pPr = p._p.get_or_add_pPr()
    pbdr = pPr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        pPr.append(pbdr)
    el = OxmlElement("w:" + side)
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), str(color).lstrip("#"))
    pbdr.append(el)


def _add_field(p, instr, placeholder="1", size=10, color="808080", font=None):
    """插入 Word 域（页码 / 目录）。域要 打开 → 指令 → 分隔 → 占位 → 结束。"""
    r = p.add_run()
    b = OxmlElement("w:fldChar")
    b.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instr
    s = OxmlElement("w:fldChar")
    s.set(qn("w:fldCharType"), "separate")
    t = OxmlElement("w:t")
    t.text = placeholder
    e = OxmlElement("w:fldChar")
    e.set(qn("w:fldCharType"), "end")
    for x in (b, it, s, t, e):
        r._r.append(x)
    _set_font(r, size, color, font=font)
    return r


def _spacing(p, before=0, after=6, line=1.4):
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = line


def _outline(p, level):
    """给段落设大纲级别，目录才能抓到它。"""
    pPr = p._p.get_or_add_pPr()
    el = OxmlElement("w:outlineLvl")
    el.set(qn("w:val"), str(max(0, min(8, level))))
    pPr.append(el)


_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)")


def _add_runs(p, text, size, color, font, bold=False, italic=False):
    """把 `**粗**` / `*斜*` / `` `码` `` 解析成多个 run，其余为普通文本。"""
    for seg in _INLINE.split(str(text or "")):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            r = p.add_run(seg[2:-2])
            _set_font(r, size, color, bold=True, font=font)
        elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
            r = p.add_run(seg[1:-1])
            _set_font(r, size, color, bold=bold, font=font, mono=True)
        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
            r = p.add_run(seg[1:-1])
            _set_font(r, size, color, bold=bold, font=font, italic=True)
        else:
            r = p.add_run(seg)
            _set_font(r, size, color, bold=bold, font=font, italic=italic)
    return p


def _bullets_of(items):
    """把要点规整成 [(层级, 文本)]。层级靠前导缩进 / 短横线判断。"""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            txt, lvl = str(it.get("text") or ""), int(it.get("level") or 0)
        else:
            raw = str(it or "")
            lvl = 1 if re.match(r"^\s{2,}|^\s*[-–—•]\s", raw) else 0
            txt = re.sub(r"^\s{2,}|^\s*[-–—•]\s", "", raw)
        txt = txt.strip()
        if txt:
            out.append((min(lvl, 2), txt))
    return out


def _fit_image(path, max_w_cm=15.5, max_h_cm=19.0):
    """按原图比例算出合适的显示宽度（cm），太大的图不会被撑爆版面。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return max_w_cm
    if not w or not h:
        return max_w_cm
    w_cm = max_w_cm
    h_cm = w_cm * h / float(w)
    if h_cm > max_h_cm:
        w_cm = max_h_cm * w / float(h)
    return w_cm


# --------------------------------------------------------------------------
# 各块渲染
# --------------------------------------------------------------------------
def _render_heading(doc, b, ctx, level):
    text = str(b.get("text") or "").strip()
    if not text:
        return
    st = b.get("style") or {}
    size = st.get("size") or {1: 20, 2: 16, 3: 14, 4: 12.5}[level]
    color = st.get("color") or (ctx["th"]["head"] if level <= 2 else ctx["th"]["text"])
    p = doc.add_paragraph()
    _spacing(p, before=16 if level == 1 else 12, after=7, line=1.25)
    if st.get("align"):
        p.alignment = _ALIGN.get(st["align"], WD_ALIGN_PARAGRAPH.LEFT)
    _add_runs(p, text, size, color, ctx["head_font"], bold=True)
    _outline(p, level - 1)          # 目录靠这个抓标题
    if level == 1 and ctx.get("head_rule", True):
        _para_border(p, "bottom", ctx["th"]["line"], size=8, space=4)


def _render_para(doc, b, ctx):
    text = str(b.get("text") or "")
    if not text.strip():
        return
    st = b.get("style") or {}
    size = st.get("size") or ctx["body_size"]
    color = st.get("color") or ctx["th"]["text"]
    p = doc.add_paragraph()
    p.alignment = _ALIGN.get(st.get("align") or ctx.get("align") or "justify",
                             WD_ALIGN_PARAGRAPH.JUSTIFY)
    _spacing(p, before=st.get("before") or 0, after=st.get("after") or 7,
             line=st.get("line") or 1.45)
    if st.get("indent_first"):
        p.paragraph_format.first_line_indent = Pt(size * 2)
    if st.get("indent"):
        p.paragraph_format.left_indent = Cm(float(st["indent"]))
    if st.get("bg"):
        _shade(p._p.get_or_add_pPr(), st["bg"])
    _add_runs(p, text, size, color, ctx["body_font"], bold=bool(st.get("bold")))


def _render_list(doc, b, ctx, ordered=False):
    items = _bullets_of(b.get("items") or b.get("text") or [])
    st = b.get("style") or {}
    size = st.get("size") or ctx["body_size"]
    color = st.get("color") or ctx["th"]["text"]
    for i, (lvl, txt) in enumerate(items):
        p = doc.add_paragraph()
        _spacing(p, before=0, after=4 if lvl else 6, line=1.35)
        pf = p.paragraph_format
        pf.left_indent = Cm(0.75 + lvl * 0.7)
        pf.first_line_indent = Cm(-0.45)
        mark = ("%d. " % (i + 1)) if (ordered and lvl == 0) else ("• " if lvl == 0 else "– ")
        r = p.add_run(mark)
        _set_font(r, size, ctx["th"]["accent2"] if lvl == 0 else ctx["th"]["muted"])
        _add_runs(p, txt, size - (1 if lvl else 0),
                  color if lvl == 0 else ctx["th"]["muted"], ctx["body_font"])


def _render_quote(doc, b, ctx):
    text = str(b.get("text") or "").strip()
    if not text:
        return
    st = b.get("style") or {}
    size = st.get("size") or (ctx["body_size"] + 1)
    p = doc.add_paragraph()
    _spacing(p, before=6, after=10, line=1.4)
    pf = p.paragraph_format
    pf.left_indent = Cm(0.6)
    pf.right_indent = Cm(0.4)
    _para_border(p, "left", st.get("color") or ctx["th"]["accent"], size=24, space=10)
    _shade(p._p.get_or_add_pPr(), st.get("bg") or ctx["th"]["quote_bg"])
    _add_runs(p, text, size, st.get("color") or ctx["th"]["text"],
              ctx["body_font"], italic=True)
    src = str(b.get("from") or "").strip()
    if src:
        p2 = doc.add_paragraph()
        _spacing(p2, before=0, after=10, line=1.2)
        p2.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p2.paragraph_format.right_indent = Cm(0.4)
        _add_runs(p2, "—— " + src, size - 2, ctx["th"]["muted"], ctx["body_font"])


def _render_callout(doc, b, ctx):
    """提示框：底色 + 左边色条 + 可选小标题（注意 / 提示 / 结论）。"""
    st = b.get("style") or {}
    size = st.get("size") or ctx["body_size"]
    tag = str(b.get("tag") or b.get("title") or "").strip()
    lines = []
    if tag:
        lines.append(("**%s**" % tag, True))
    txt = str(b.get("text") or "")
    if txt:
        lines.append((txt, False))
    if not lines:
        return
    for i, (line, _is_tag) in enumerate(lines):
        p = doc.add_paragraph()
        _spacing(p, before=6 if i == 0 else 0, after=8 if i == len(lines) - 1 else 2,
                 line=1.4)
        pf = p.paragraph_format
        pf.left_indent = Cm(0.5)
        pf.right_indent = Cm(0.3)
        _shade(p._p.get_or_add_pPr(), st.get("bg") or ctx["th"]["callout_bg"])
        if i == 0:
            _para_border(p, "left", st.get("color") or ctx["th"]["accent"], 24, 10)
        _add_runs(p, line, size, st.get("color") or ctx["th"]["text"], ctx["body_font"])


def _render_code(doc, b, ctx):
    txt = str(b.get("text") or "").rstrip()
    if not txt:
        return
    st = b.get("style") or {}
    size = st.get("size") or 9.5
    for i, line in enumerate(txt.split("\n")):
        p = doc.add_paragraph()
        _spacing(p, before=4 if i == 0 else 0, after=8 if i == len(txt.split("\n")) - 1 else 0,
                 line=1.15)
        pf = p.paragraph_format
        pf.left_indent = Cm(0.5)
        pf.right_indent = Cm(0.3)
        _shade(p._p.get_or_add_pPr(), st.get("bg") or "F6F7F9")
        if i == 0:
            _para_border(p, "left", ctx["th"]["accent2"], 16, 8)
        r = p.add_run(line if line else " ")
        _set_font(r, size, st.get("color") or "1F2933", mono=True)


def _render_table(doc, b, ctx):
    header = [str(x) for x in (b.get("header") or [])]
    rows = [[str(c) for c in r] for r in (b.get("rows") or [])]
    if not header and not rows:
        return
    st = b.get("style") or {}
    size = st.get("size") or 10
    ncol = max([len(header)] + [len(r) for r in rows] or [0])
    if not ncol:
        return
    cap = str(b.get("caption") or "").strip()
    if cap:
        p = doc.add_paragraph()
        _spacing(p, before=8, after=4, line=1.2)
        _add_runs(p, cap, size + 0.5, ctx["th"]["muted"], ctx["body_font"], bold=True)

    t = doc.add_table(rows=0, cols=ncol)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    if header:
        cells = t.add_row().cells
        for i in range(ncol):
            c = cells[i]
            c.text = ""
            pr = c.paragraphs[0]
            pr.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _spacing(pr, before=3, after=3, line=1.15)
            _add_runs(pr, header[i] if i < len(header) else "", size,
                      "FFFFFF", ctx["body_font"], bold=True)
            _shade(c._tc.get_or_add_tcPr(), st.get("accent") or ctx["th"]["accent"])
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i in range(ncol):
            c = cells[i]
            c.text = ""
            pr = c.paragraphs[0]
            _spacing(pr, before=2, after=2, line=1.2)
            _add_runs(pr, row[i] if i < len(row) else "", size,
                      ctx["th"]["text"], ctx["body_font"])
            if ri % 2 == 1:
                _shade(c._tc.get_or_add_tcPr(), st.get("zebra") or "F7F9FB")
    p = doc.add_paragraph()
    _spacing(p, before=0, after=8, line=1.0)


def _render_image(doc, b, ctx):
    src = str(b.get("src") or b.get("path") or "").strip()
    query = str(b.get("query") or b.get("image_query") or "").strip()
    if not src and not query:
        return
    # 没给图、只给了搜索词 → 联网找一张（结果按关键词缓存，不会重复搜）
    # ⚠️ src 里也可能直接写的是搜索词，所以统一走 resolve_or_search。
    from . import img_fetch
    used = ctx.setdefault("used_imgs", set())
    path = img_fetch.resolve(src, ctx["bases"]) if src else ""
    if path and path in used:                  # 这份文稿里已经用过这张了
        path = ""
    if not path and (src or query):
        for cand in (query, src):
            c = img_fetch.as_query(cand)
            if not c:
                continue
            path = img_fetch.search_one(c, exclude=used)
            if path:
                break
    if path:
        used.add(path)
    if path and not src:
        ctx.setdefault("fetched", []).append(query)
    if not path or not os.path.exists(path):
        ctx["warnings"].append("图片找不到：%s" % (src or query))
        return
    st = b.get("style") or {}
    width = st.get("width")
    w_cm = float(width) if width else _fit_image(path)
    w_cm = max(1.0, min(w_cm, 16.0))
    cap = str(b.get("caption") or "").strip()
    try:
        p = doc.add_paragraph()
        p.alignment = _ALIGN.get(st.get("align") or "center", WD_ALIGN_PARAGRAPH.CENTER)
        _spacing(p, before=6, after=3 if cap else 8, line=1.0)
        p.add_run().add_picture(path, width=Cm(w_cm))
    except Exception as e:
        ctx["warnings"].append("插入图片失败（%s）：%s" % (src, e))
        return
    if cap:
        pc = doc.add_paragraph()
        pc.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(pc, before=0, after=10, line=1.2)
        _add_runs(pc, cap, max(8.5, ctx["body_size"] - 2), ctx["th"]["muted"],
                  ctx["body_font"])


def _render_divider(doc, ctx):
    p = doc.add_paragraph()
    _spacing(p, before=4, after=8, line=1.0)
    _para_border(p, "bottom", ctx["th"]["line"], size=8, space=2)
    r = p.add_run(" ")
    _set_font(r, 2, ctx["th"]["text"])


def _mark_update_fields(doc):
    """让 Word/WPS **打开时自动更新域**（目录、页码）。

    ⚠️ 没有这一条，目录域只会显示我们塞的占位文字（"打开后自动生成"），
    用户会以为目录坏了。`w:updateFields` 是标准做法。
    """
    try:
        el = OxmlElement("w:updateFields")
        el.set(qn("w:val"), "true")
        doc.settings.element.append(el)
    except Exception:
        pass


def _render_toc(doc, b, ctx):
    ctx["want_toc"] = True
    p = doc.add_paragraph()
    _spacing(p, before=4, after=10, line=1.3)
    _add_runs(p, str(b.get("text") or "目录"), 15, ctx["th"]["head"],
              ctx["head_font"], bold=True)
    pd = doc.add_paragraph()
    _spacing(pd, before=0, after=6, line=1.4)
    # TOC 域：\o "1-3" 收 1~3 级标题；\h 做成超链接
    _add_field(pd, 'TOC \\o "1-3" \\h \\z \\u',
               placeholder="（打开文档会自动生成目录）",
               size=ctx["body_size"], color=ctx["th"]["muted"], font=ctx["body_font"])
    doc.add_page_break()


def _render_end(doc, b, ctx):
    txt = str(b.get("text") or "— 完 —").strip()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(p, before=18, after=6, line=1.3)
    _add_runs(p, txt, ctx["body_size"] + 1, ctx["th"]["muted"], ctx["body_font"])


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def _resolve(src, bases):
    """把模型写的图片来源变成本地文件（绝对路径 / 网址 / 图库 id / 文件名都认）。

    统一走 `img_fetch`：网图会自动下载并缓存到本地，webp 会转成 png
    （python-docx 不认 webp）。细节见 img_fetch 模块说明。
    """
    from . import img_fetch
    return img_fetch.resolve(src, bases)


def _apply_header_footer(doc, ctx, text, skip_first=False, logo=""):
    """页眉（可带 logo）+ 页脚页码。域不会自动算，交给 Word/WPS 打开时更新。

    skip_first=True 时首页（封面）不显示页眉页码 —— 规范做法。
    """
    logo_path = _resolve(logo, ctx.get("bases")) if str(logo or "").strip() else ""
    for sec in doc.sections:
        if skip_first:
            try:
                sec.different_first_page_header_footer = True
            except Exception:
                pass
        # logo 放页眉最右，正文页眉居中另起一段 —— 同一段里两种对齐做不到
        last = None
        if logo_path and os.path.exists(logo_path):
            lp = sec.header.paragraphs[0]
            lp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _spacing(lp, before=0, after=0, line=1.0)
            try:
                lp.add_run().add_picture(logo_path, height=Pt(16))
                last = lp
            except Exception:
                pass
        if text:
            hp = (sec.header.add_paragraph()
                  if (last is not None) else sec.header.paragraphs[0])
            hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _spacing(hp, before=0, after=0, line=1.0)
            _add_runs(hp, text, 9, ctx["th"]["muted"], ctx["body_font"])
            last = hp
        if last is not None:
            _para_border(last, "bottom", ctx["th"]["line"], size=6, space=4)
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(fp, before=0, after=0, line=1.0)
        _add_field(fp, "PAGE", placeholder="1", size=9,
                   color=ctx["th"]["muted"], font=ctx["body_font"])


def make_ctx(theme=DEFAULT_THEME, font=DEFAULT_FONT, bases=None, body_size=11.0):
    """构造渲染上下文。office_edit 往已有文档里追加内容时要用同一个。"""
    return {
        "th": THEMES.get(str(theme or "").strip().lower(), THEMES[DEFAULT_THEME]),
        "body_font": FONT_SETS.get(str(font or "").strip().lower(),
                                   FONT_SETS[DEFAULT_FONT])[0],
        "head_font": FONT_SETS.get(str(font or "").strip().lower(),
                                   FONT_SETS[DEFAULT_FONT])[1],
        "body_size": float(body_size or 11.0), "warnings": [],
        "bases": [b for b in (bases or []) if b],
        "align": "justify", "head_rule": True,
    }


_BLOCK_DISPATCH = {
    "heading": lambda d, b, c: _render_heading(d, b, c, int(b.get("level") or 1)),
    "h1": lambda d, b, c: _render_heading(d, b, c, 1),
    "h2": lambda d, b, c: _render_heading(d, b, c, 2),
    "h3": lambda d, b, c: _render_heading(d, b, c, 3),
    "h4": lambda d, b, c: _render_heading(d, b, c, 4),
    "para": _render_para,
    "bullet": lambda d, b, c: _render_list(d, b, c, False),
    "number": lambda d, b, c: _render_list(d, b, c, True),
    "quote": _render_quote,
    "callout": _render_callout,
    "table": _render_table,
    "image": _render_image,
    "code": _render_code,
    "divider": lambda d, b, c: _render_divider(d, c),
    "pagebreak": lambda d, b, c: d.add_page_break(),
    "toc": _render_toc,
    "end": _render_end,
}
_BLOCK_ALIAS = {
    "title": "heading", "h": "heading", "p": "para", "text": "para",
    "paragraph": "para", "bullets": "bullet", "ul": "bullet", "list": "bullet",
    "numbers": "number", "ol": "number", "ordered": "number",
    "blockquote": "quote", "note": "callout", "tip": "callout",
    "warn": "callout", "box": "callout", "grid": "table", "img": "image",
    "picture": "image", "figure": "image", "pre": "code", "mono": "code",
    "hr": "divider", "line": "divider", "page_break": "pagebreak",
    "newpage": "pagebreak", "outline": "toc", "ending": "end",
}


def add_block(doc, block, ctx):
    """往已打开的 docx 续写一个内容块（office_edit 追加内容时用）。"""
    if not isinstance(block, dict):
        return None
    t = str(block.get("type") or "").strip().lower()
    if not t:
        t = "bullet" if block.get("items") else "para"
    t = _BLOCK_ALIAS.get(t, t)
    fn = _BLOCK_DISPATCH.get(t)
    if fn is None:
        ctx["warnings"].append("不认识的内容块类型「%s」，已跳过" % t)
        return None
    fn(doc, block, ctx)
    return t


def _add_cover(doc, ctx, title, subtitle, author, date_text):
    th = ctx["th"]
    for _ in range(4):
        p = doc.add_paragraph()
        _spacing(p, before=0, after=0, line=1.0)
        _set_font(p.add_run(" "), 12, th["text"])
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(p, before=0, after=12, line=1.3)
    _add_runs(p, title, 30 if len(title) <= 18 else 24, th["head"],
              ctx["head_font"], bold=True)
    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(p2, before=0, after=10, line=1.2)
    _para_border(p2, "bottom", th["accent"], size=12, space=8)
    _set_font(p2.add_run(" "), 8, th["text"])
    if subtitle:
        p3 = doc.add_paragraph()
        p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(p3, before=6, after=10, line=1.3)
        _add_runs(p3, subtitle, 13, th["muted"], ctx["body_font"])
    for _ in range(6):
        p4 = doc.add_paragraph()
        _spacing(p4, before=0, after=0, line=1.0)
        _set_font(p4.add_run(" "), 12, th["text"])
    for line in [x for x in (author, date_text) if x]:
        p5 = doc.add_paragraph()
        p5.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(p5, before=0, after=5, line=1.2)
        _add_runs(p5, line, 11.5, th["muted"], ctx["body_font"])
    doc.add_page_break()


def markdown_blocks(text):
    """把 Markdown 风格的文本解析成内容块列表。

    用途：用户/模型**先把正文写成 .md 存进文库**，再转 Word。
    这种情况下不该丢排版 —— `#` 标题、`- ` 列表、`1. ` 编号、`> ` 引用、
    `| a | b |` 表格、``` 代码块、`---` 分隔线，都认。
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, buf, i = [], [], 0

    def flush():
        if buf:
            blocks.append({"type": "para", "text": " ".join(buf).strip()})
            buf.clear()

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):                       # 代码块
            flush()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"type": "code", "text": "\n".join(code)})
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            blocks.append({"type": "heading", "level": len(m.group(1)),
                           "text": m.group(2).strip()})
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):     # 分隔线
            flush()
            blocks.append({"type": "divider"})
            i += 1
            continue
        if s.startswith("|") and s.count("|") >= 2:    # 表格
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[:\-\s|]+$", "".join(cells)):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append({"type": "table", "header": rows[0],
                               "rows": rows[1:]})
            continue
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", ln):    # 列表
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", ln))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
                raw = lines[i]
                indent = len(raw) - len(raw.lstrip())
                txt = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", raw).strip()
                items.append(("  " + txt) if indent >= 2 else txt)
                i += 1
            blocks.append({"type": "number" if ordered else "bullet",
                           "items": items})
            continue
        if s.startswith(">"):                          # 引用
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "quote", "text": " ".join(quote)})
            continue
        buf.append(s)
        i += 1
    flush()
    return blocks


def build_docx_text(path, title, text, **kw):
    """把一个 Markdown 风格文本文件**排成正经 Word 文档**（export_docx 用）。"""
    return build_docx(path, title, markdown_blocks(text), **kw)


def build_docx(path, title, blocks, subtitle="", author="", date_text="",
               theme=DEFAULT_THEME, font=DEFAULT_FONT, cover=False,
               header="", toc=False, margins=2.5, bases=None, logo=""):
    """把结构化内容生成 docx，返回 {'ok','path','blocks','pages','warnings','error'}。

    blocks 每项：{'type': ..., 其余字段见模块 docstring}
    每个块都可以带 'style' 做微调：size / color / bold / align / indent /
    bg / line / before / after / width(图片，cm)
    """
    try:
        ctx = make_ctx(theme, font, bases)
        warnings = ctx["warnings"]
        th = ctx["th"]
        body_font = ctx["body_font"]

        doc = Document()
        # 页面
        for sec in doc.sections:
            sec.page_width, sec.page_height = PAGE_W, PAGE_H
            sec.left_margin = sec.right_margin = Cm(float(margins))
            sec.top_margin = Cm(2.5)
            sec.bottom_margin = Cm(2.3)
        # 默认字体（含中文）
        _style_font(doc.styles["Normal"], body_font)
        doc.styles["Normal"].font.size = Pt(11)
        try:
            _style_font(doc.styles["Table Grid"], body_font)
        except Exception:
            pass

        if cover:
            _add_cover(doc, ctx, str(title or "文档"), str(subtitle or ""),
                       str(author or ""), str(date_text or ""))
        elif title:
            _render_heading(doc, {"text": str(title)}, ctx, 1)

        if toc:
            _render_toc(doc, {}, ctx)

        n = 0
        for b in (blocks or []):
            if add_block(doc, b, ctx):
                n += 1

        if cover or header or logo:
            _apply_header_footer(doc, ctx, str(header or ""), skip_first=bool(cover),
                                 logo=str(logo or ""))
        if toc or ctx.get("want_toc"):
            _mark_update_fields(doc)

        out = str(path or "")
        if not out.lower().endswith(".docx"):
            out += ".docx"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        doc.save(out)
        return {"ok": True, "path": out, "blocks": n, "warnings": warnings,
                "error": ""}
    except Exception as e:
        return {"ok": False, "path": "", "blocks": 0, "warnings": warnings,
                "error": "%s: %s" % (type(e).__name__, e)}


