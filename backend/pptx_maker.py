# -*- coding: utf-8 -*-
"""PPT 生成：把结构化内容排成 .pptx 文件。

设计原则
--------
1. **模型只填内容，不写代码。** 让 8B 模型即兴写 python-pptx 代码很容易出错，
   而且每次效果都不一样。这里让它只输出「标题 + 要点」，排版交给本模块，
   稳定性高一个量级。
2. **通用，不绑定领域。** 课程汇报、技术方案、商业计划、读书笔记、项目复盘
   都能排 —— 版式是中性的，靠内容决定风格。
3. ⚠️ **中文字体必须显式设 `a:ea`（East Asian）**，python-pptx 只设 `font.name`
   管不到中文，WPS/Office 里会掉回宋体甚至豆腐块。

版式
----
- 封面页：整屏主题色 + 居中大标题 + 副标题 + 底部落款
- 章节页（可选）：整屏淡色 + 大号章节序号与标题
- 内容页：顶部标题 + 强调色短横线 + 要点列表（支持两级）
- 结尾页（可选）：整屏主题色 + 居中结束语
"""

import os
import re
import time

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

# 16:9
SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)

# 每个主题：封面底色 / 封面文字 / 内页文字 / 强调色 / 内页背景
THEMES = {
    "blue": {
        "cover_bg": "1F4E79", "cover_fg": "FFFFFF",
        "body": "2B2B2B", "accent": "2E75B6", "bg": "FFFFFF",
    },
    "green": {
        "cover_bg": "2D6A4F", "cover_fg": "FFFFFF",
        "body": "262626", "accent": "40916C", "bg": "FFFFFF",
    },
    "warm": {
        "cover_bg": "B45309", "cover_fg": "FFFFFF",
        "body": "2B2B2B", "accent": "EA8C3A", "bg": "FFFDF8",
    },
    "purple": {
        "cover_bg": "4C3A8C", "cover_fg": "FFFFFF",
        "body": "2B2B2B", "accent": "7C6BD6", "bg": "FFFFFF",
    },
    "mono": {
        "cover_bg": "262626", "cover_fg": "FFFFFF",
        "body": "1F1F1F", "accent": "808080", "bg": "FFFFFF",
    },
    "red": {
        "cover_bg": "9B2C2C", "cover_fg": "FFFFFF",
        "body": "2B2B2B", "accent": "C53030", "bg": "FFFFFF",
    },
}

DEFAULT_THEME = "blue"
_CN_FONT = "微软雅黑"


def _rgb(s: str) -> RGBColor:
    s = (s or "000000").lstrip("#")
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _set_font(run, size: int, color: str, bold: bool = False, font: str = _CN_FONT):
    """统一设置字体 —— **中英文都要设**，只设 font.name 管不到中文。

    ⚠️ python-pptx 的 Run 对象取 XML 元素是 `run._r`（不是 `_element`，
    那是 lxml 的写法），搞错会报 `'_Run' object has no attribute '_element'`。
    """
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = _rgb(color)
    run.font.name = font                      # 这一句会自动建好 <a:latin>
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):              # 中日韩文字 / 复杂文种
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", font)


def _blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])   # 6 = 完全空白


def _fill_bg(slide, color: str):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(color)


def _textbox(slide, x, y, w, h):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    return tf


def _add_cover(prs, th, title, subtitle, author):
    s = _blank(prs)
    _fill_bg(s, th["cover_bg"])
    tf = _textbox(s, Inches(0.9), Inches(2.35), Inches(11.5), Inches(2.6))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _set_font(p.add_run(), 1, th["cover_fg"])       # 占位，避免空段落
    p.runs[0].text = title[:80]
    _set_font(p.runs[0], 40 if len(title) <= 18 else 32, th["cover_fg"], bold=True)
    if subtitle:
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        p2.space_before = Pt(16)
        r = p2.add_run()
        r.text = subtitle[:60]
        _set_font(r, 18, th["cover_fg"])
    if author:
        tf2 = _textbox(s, Inches(0.9), Inches(6.25), Inches(11.5), Inches(0.7))
        p3 = tf2.paragraphs[0]
        p3.alignment = PP_ALIGN.CENTER
        r3 = p3.add_run()
        r3.text = author[:60]
        _set_font(r3, 13, th["cover_fg"])
    return s


def _add_section(prs, th, text, index):
    """章节过渡页：整屏淡色 + 大号序号与标题。"""
    s = _blank(prs)
    _fill_bg(s, th["bg"])
    bar = s.shapes.add_shape(1, Inches(0), Inches(3.05), Inches(0.28), Inches(1.4))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(th["accent"])
    bar.line.fill.background()
    tf = _textbox(s, Inches(0.95), Inches(3.05), Inches(11.4), Inches(1.5))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = "%02d" % index
    _set_font(r, 40, th["accent"], bold=True)
    p2 = tf.add_paragraph()
    r2 = p2.add_run()
    r2.text = text[:60]
    _set_font(r2, 30, th["body"], bold=True)
    return s


def _bullets_of(items):
    """把要点规整成 [(层级, 文本)]。层级靠前导缩进 / 短横线判断。"""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            txt, lvl = str(it.get("text") or ""), int(it.get("level") or 0)
        else:
            raw = str(it or "")
            lvl = 1 if re.match(r"^\s{2,}|^\s*[-–—]\s", raw) else 0
            txt = re.sub(r"^\s{2,}|^\s*[-–—]\s", "", raw)
        txt = txt.strip()
        if txt:
            out.append((min(lvl, 1), txt))
    return out


def _add_content(prs, th, title, bullets, notes):
    s = _blank(prs)
    _fill_bg(s, th["bg"])

    tf = _textbox(s, Inches(0.85), Inches(0.5), Inches(11.7), Inches(0.9))
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = title[:50] or " "
    _set_font(r, 28, th["body"], bold=True)

    bar = s.shapes.add_shape(1, Inches(0.88), Inches(1.42), Inches(1.5), Inches(0.07))
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(th["accent"])
    bar.line.fill.background()

    bs = _bullets_of(bullets)
    n = len(bs)
    # 要点越多字号越小，保证不溢出
    size = 20 if n <= 4 else 18 if n <= 6 else 16 if n <= 9 else 14
    # 要点少的时候把整块**垂直居中** —— 否则只有三行字的话，
    # 下半页会空一大片，看着像没做完（实测导出图片后发现的）。
    line_h = (size * 1.65 + 9) / 72.0            # 英寸：行高 + 段间距
    est_h = max(1.0, n * line_h)
    body_top = 1.85 if est_h >= 4.5 else 1.85 + (5.0 - est_h) / 2.0
    tf2 = _textbox(s, Inches(0.95), Inches(body_top), Inches(11.5),
                   Inches(min(est_h + 0.7, 5.2)))
    tf2.paragraphs[0].text = ""
    first = True
    for lvl, txt in bs:
        para = tf2.paragraphs[0] if first else tf2.add_paragraph()
        first = False
        para.level = lvl
        para.space_before = Pt(4 if lvl else 9)
        para.line_spacing = 1.15
        run = para.add_run()
        run.text = ("• " if lvl == 0 else "– ") + txt[:180]
        _set_font(run, size - (2 if lvl else 0),
                  th["body"] if lvl == 0 else "595959",
                  bold=False)
    if notes:
        s.notes_slide.notes_text_frame.text = str(notes)[:2000]
    return s


def _add_end(prs, th, text):
    s = _blank(prs)
    _fill_bg(s, th["cover_bg"])
    tf = _textbox(s, Inches(1.5), Inches(3.15), Inches(10.3), Inches(1.3))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text[:40] or "谢谢观看"
    _set_font(r, 36, th["cover_fg"], bold=True)
    return s


def build_pptx(path: str, title: str, slides: list, subtitle: str = "",
               author: str = "", theme: str = DEFAULT_THEME,
               end_text: str = "") -> dict:
    """把结构化内容生成 pptx，返回 {'ok', 'path', 'slides', 'error'}。

    slides 每项：{'title': 页标题, 'bullets': [要点...], 'notes': 备注,
                  'section': True 表示这是章节过渡页}
    要点前加两个空格或「- 」即为二级要点。
    """
    try:
        th = THEMES.get((theme or "").strip().lower(), THEMES[DEFAULT_THEME])
        prs = Presentation()
        prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H

        _add_cover(prs, th, str(title or "演示文稿"), str(subtitle or ""), str(author or ""))

        sec = 0
        made = 0
        for sl in (slides or []):
            if not isinstance(sl, dict):
                continue
            t = str(sl.get("title") or "").strip()
            bl = sl.get("bullets") or []
            nt = sl.get("notes") or ""
            if sl.get("section") or (t and not bl):
                sec += 1
                _add_section(prs, th, t or "章节", sec)
            else:
                _add_content(prs, th, t, bl, nt)
            made += 1

        if end_text or end_text == "":
            _add_end(prs, th, end_text or "谢谢观看")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        prs.save(path)
        return {"ok": True, "path": path, "slides": made + 1, "error": ""}
    except Exception as e:
        return {"ok": False, "path": "", "slides": 0,
                "error": "%s: %s" % (type(e).__name__, e)}
