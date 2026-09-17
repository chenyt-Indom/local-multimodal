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
                        "mark": extra.get("mark")})
    return out


def _fit_size(n, hi=20, mid=18, lo=16, tiny=14):
    return hi if n <= 4 else mid if n <= 6 else lo if n <= 9 else tiny


def _put_bullets(slide, bs, x, y, w, h, th, st, base_size=None, font=None,
                 centered=False, valign="auto"):
    """把要点列表铺进一个文本框。

    valign="auto"：要点少时整块垂直居中（单栏页面不留大片空白）；
    valign="top" ：始终顶对齐（分栏页面各栏要对齐，居中会显得错落）。
    """
    n = len(bs)
    size = float(base_size or _fit_size(n))
    line_h = (size * 1.62 + 9) / 72.0
    est = max(1.0, n * line_h)
    if valign == "top" or est >= h * 0.92:
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
        r = p.add_run()
        r.text = b["text"][:200]
        _set_font(r, sz, col, bold=bool(b.get("bold")), font=font,
                  italic=bool(b.get("italic")))
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


def _title_bar(slide, title, th, st, y=Inches(0.5), rule=True):
    """统一的内页标题：标题 + 下方强调短横线。"""
    tf = _textbox(slide, Inches(0.85), y, Inches(11.7), Inches(0.9))
    p = tf.paragraphs[0]
    if st.get("title_align"):
        p.alignment = _ALIGN.get(st["title_align"], PP_ALIGN.LEFT)
    r = p.add_run()
    r.text = (title or " ")[:60]
    _set_font(r, float(st.get("title_size") or 28),
              st.get("title_color") or th["body"], bold=True, font=st.get("font"))
    if rule:
        _rect(slide, Inches(0.88), y + Inches(0.92), Inches(1.5), Inches(0.07),
              fill=st.get("accent") or th["accent"])
    return y + Inches(1.15)


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
    r.text = title[:80]
    _set_font(r, 40 if len(title) <= 18 else 32, th["cover_fg"], bold=True,
              font=st.get("font"))
    if subtitle:
        p2 = tf.add_paragraph()
        p2.alignment = p.alignment
        p2.space_before = Pt(16)
        r2 = p2.add_run()
        r2.text = subtitle[:60]
        _set_font(r2, 18, _tint(th["cover_fg"], 0.22), font=st.get("font"))
    if author:
        tf2 = _textbox(s, Inches(0.9), Inches(6.25), Inches(11.5), Inches(0.7))
        p3 = tf2.paragraphs[0]
        p3.alignment = p.alignment
        r3 = p3.add_run()
        r3.text = author[:60]
        _set_font(r3, 13, _tint(th["cover_fg"], 0.3), font=st.get("font"))
    return s


def _add_section(prs, th, st, text, index, page_no):
    s = _blank(prs)
    # 章节页没有独立的页配置（参数是标题文字），传空字典走默认底
    _page_base(s, th, st, {}, page_no)
    _rect(s, Inches(0), Inches(3.05), Inches(0.28), Inches(1.4),
          fill=st.get("accent") or th["accent"])
    tf = _textbox(s, Inches(0.95), Inches(3.05), Inches(11.4), Inches(1.5))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = "%02d" % index
    _set_font(r, 40, st.get("accent") or th["accent"], bold=True, font=st.get("font"))
    p2 = tf.add_paragraph()
    r2 = p2.add_run()
    r2.text = text[:60]
    _set_font(r2, 30, st.get("title_color") or th["body"], bold=True,
              font=st.get("font"))
    return s


def _add_content(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st, rule=bool(st.get("rule", True)))
    bs = _bullets_of(sl.get("bullets"))
    _put_bullets(s, bs, Inches(0.95), y + Inches(0.4), Inches(11.45),
                 SLIDE_H - y - Inches(0.9), th, st,
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
    for i, (cx, head, items) in enumerate((
            (Inches(0.9), lh, left), (Inches(0.9) + colw + gap, rh, right))):
        top = y + Inches(0.35)
        if head:
            tfh = _textbox(s, cx, top, colw, Inches(0.5))
            ph = tfh.paragraphs[0]
            rh2 = ph.add_run()
            rh2.text = head[:30]
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
            # 竖向分隔线：**只跟到内容底部**，拉到底会显得左栏"没写完"
            dv_h = min(Inches(3.3), SLIDE_H - y - Inches(1.4))
            _rect(s, Inches(0.9) + colw + Inches(0.18), y + Inches(0.4),
                  Inches(0.02), dv_h, fill=_tint(th["accent"], 0.75))
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
        rc.text = cap[:60]
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
    r.text = (sl.get("title") or " ")[:60]
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
        return _add_content(prs, th, st, sl, page_no)
    ncol = max([len(header)] + [len(r) for r in rows] or [0])
    nrow = (1 if header else 0) + len(rows)
    acc = st.get("accent") or th["accent"]
    size = float(st.get("body_size") or (16 if ncol <= 4 else 14 if ncol <= 6 else 12))
    avail_h = SLIDE_H - y - Inches(1.0)
    row_h = min(Inches(0.62), max(Inches(0.36), avail_h // max(1, nrow)))
    gf = s.shapes.add_table(nrow, ncol, Inches(0.9), y + Inches(0.4),
                            Inches(11.5), row_h * nrow)
    tb = gf.table
    _no_table_style(tb)
    for i in range(ncol):
        tb.columns[i].width = int(Inches(11.5) / ncol)
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
            r = p.add_run()
            r.text = (header[ci] if ci < len(header) else "")[:40]
            _set_font(r, size, "FFFFFF", bold=True, font=st.get("font"))
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
            r = p.add_run()
            r.text = (row[ci] if ci < len(row) else "")[:60]
            _set_font(r, size, th["body"], font=st.get("font"))
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
        return _add_content(prs, th, st, sl, page_no)

    from pptx.chart.data import CategoryChartData
    data = CategoryChartData()
    data.categories = cats
    for sx in series[:4]:
        vals = []
        for v in (sx.get("values") or []):
            try:
                vals.append(float(v))
            except Exception:
                vals.append(0.0)
        data.add_series(str(sx.get("name") or "系列")[:20], vals)

    left, top = Inches(0.9), y + Inches(0.45)
    width = Inches(11.5)
    height = SLIDE_H - top - Inches(0.95)
    try:
        gf = s.shapes.add_chart(_chart_kind(spec.get("kind")), left, top,
                                width, height, data)
    except Exception as e:
        return _add_content(prs, th, st, sl, page_no)
    ch = gf.chart
    acc = st.get("accent") or th["accent"]

    if spec.get("title"):
        ch.has_title = True
        try:
            ch.chart_title.text_frame.text = str(spec["title"])[:40]
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
    cards = [c for c in (sl.get("cards") or []) if isinstance(c, dict)][:4]
    if not cards:
        return _add_content(prs, th, st, sl, page_no)
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
        rt = pt.add_run()
        rt.text = str(c.get("title") or "")[:24]
        _set_font(rt, 17, th["body"], bold=True, font=st.get("font"))
        pb = tf.add_paragraph()
        pb.space_before = Pt(8)
        pb.line_spacing = 1.3
        rb = pb.add_run()
        rb.text = str(c.get("text") or "")[:180]
        _set_font(rb, 13, st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_stats(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    items = [x for x in (sl.get("stats") or []) if isinstance(x, dict)][:4]
    if not items:
        return _add_content(prs, th, st, sl, page_no)
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
        rv.text = str(it.get("value") or "")[:12]
        _set_font(rv, 46 if len(str(it.get("value") or "")) <= 5 else 34,
                  acc, bold=True, font=st.get("font"))
        _rect(s, cx + int(cw * 0.32), top + Inches(1.45), int(cw * 0.36),
              Inches(0.05), fill=_tint(acc, 0.5))
        tfl = _textbox(s, cx, top + Inches(1.75), cw, Inches(0.9))
        pl = tfl.paragraphs[0]
        pl.alignment = PP_ALIGN.CENTER
        pl.line_spacing = 1.25
        rl = pl.add_run()
        rl.text = str(it.get("label") or "")[:40]
        _set_font(rl, 14, st.get("muted") or th["muted"], font=st.get("font"))
    if sl.get("notes"):
        s.notes_slide.notes_text_frame.text = str(sl["notes"])[:2000]
    return s


def _add_steps(prs, th, st, sl, page_no):
    s = _blank(prs)
    _page_base(s, th, st, sl, page_no)
    y = _title_bar(s, sl.get("title"), th, st)
    items = [x for x in (sl.get("steps") or []) if isinstance(x, dict)][:5]
    if not items:
        return _add_content(prs, th, st, sl, page_no)
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
        rt = pt.add_run()
        rt.text = str(it.get("title") or "")[:20]
        _set_font(rt, 16, th["body"], bold=True, font=st.get("font"))
        pb = tfb.add_paragraph()
        pb.alignment = PP_ALIGN.CENTER
        pb.space_before = Pt(9)
        pb.line_spacing = 1.3
        rb = pb.add_run()
        rb.text = str(it.get("text") or "")[:160]
        _set_font(rb, 12.5, st.get("muted") or th["muted"], font=st.get("font"))
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
    items = [x for x in (sl.get("items") or []) if isinstance(x, dict)][:5]
    if not items:
        return _add_content(prs, th, st, sl, page_no)
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
        rd = pd.add_run()
        rd.text = str(it.get("title") or "")[:16]
        _set_font(rd, 14, acc, bold=True, font=st.get("font"))
        tfb = _textbox(s, cx - Inches(1.0), axis_y + Inches(0.32), Inches(2.0),
                       Inches(1.8))
        pb = tfb.paragraphs[0]
        pb.alignment = PP_ALIGN.CENTER
        pb.line_spacing = 1.3
        rb = pb.add_run()
        rb.text = str(it.get("text") or "")[:150]
        _set_font(rb, 12, st.get("muted") or th["muted"], font=st.get("font"))
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
    q = sl.get("quote") or {}
    text = str(q.get("text") or sl.get("title") or "").strip()
    src = str(q.get("from") or "")
    _mark = _textbox(s, Inches(1.3), Inches(1.35), Inches(3.0), Inches(1.4))
    rm = _mark.paragraphs[0].add_run()
    rm.text = "“"
    _set_font(rm, 96, _tint(th["cover_fg"], 0.55), bold=True, font=st.get("font"))
    tf = _textbox(s, Inches(1.9), Inches(2.1), Inches(9.6), Inches(2.9),
                  anchor=MSO_ANCHOR.MIDDLE)
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.line_spacing = 1.35
    r = p.add_run()
    r.text = text[:160]
    _set_font(r, 30 if len(text) <= 40 else 24, th["cover_fg"], bold=True,
              font=st.get("font"))
    if src:
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        p2.space_before = Pt(20)
        r2 = p2.add_run()
        r2.text = "—— " + src[:40]
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
            rl.text = b["text"][:34]
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
    r.text = (text or "谢谢观看")[:40]
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
def build_pptx(path, title, slides, subtitle="", author="", theme=DEFAULT_THEME,
               end_text="", font=DEFAULT_FONT, page_number=True,
               cover=True, end_page=True, img_bases=None,
               logo="", cover_image="", logo_pos="tr", logo_size=0.5):
    """把结构化内容生成 pptx，返回 {'ok','path','slides','warnings','error'}。

    slides 每项：见模块 docstring。`layout` 决定版式，`decor` 加装饰，
    `style` 做本页微调（accent / bg / card_bg / title_color / title_size /
    title_align / body_size / body_align / font / decor）。
    """
    warnings = []
    try:
        th = dict(THEMES.get(str(theme or "").strip().lower(), THEMES[DEFAULT_THEME]))
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
                _add_section(prs, th, st, str(s.get("title") or "章节"), sec, made + 1)
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
