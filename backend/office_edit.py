# -*- coding: utf-8 -*-
"""已有 Word / PPT 的**读取与修改**。

分工：
- `make_docx` / `make_pptx` 负责**从零生成**；
- 本模块负责**看懂已有的**（`inspect`）和**改它**（`edit`）。

为什么要能"看懂"：模型要改一份文件，必须先知道里面有什么（第几页、第几个元素、
现在是什么文字），否则只能瞎猜 —— 猜出来的操作号往往对不上，改错地方。

⚠️ 索引约定：inspect 输出的编号就是 edit 操作的编号，两边必须用同一套遍历顺序，
否则"看着第 2 个，改到了第 3 个"。所以 `_pptx_shapes()` / `_docx_paras()`
被 inspect 和 edit **共用**。
"""

import copy
import os

# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def _emu2in(v):
    try:
        return round(int(v) / 914400.0, 2)
    except Exception:
        return 0


def _rgb_hex(color):
    try:
        return str(color.rgb)
    except Exception:
        return ""


def _has_solid_fill(shape):
    try:
        return shape.fill.type is not None and shape.fill.type == 1   # 1 = solid
    except Exception:
        return False


def _fill_hex(shape):
    try:
        if shape.fill.type == 1:
            return _rgb_hex(shape.fill.fore_color)
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# PPTX
# --------------------------------------------------------------------------
def _pptx_shapes(slide):
    """**唯一的 shape 遍历顺序** —— inspect 与 edit 都用它，保证编号一致。"""
    return list(slide.shapes)


def _shape_kind(shape):
    """给人看的形状类型名。"""
    try:
        if shape.shape_type is not None:
            nm = str(shape.shape_type)
            if "PICTURE" in nm:
                return "图片"
            if "TABLE" in nm:
                return "表格"
            if "GROUP" in nm:
                return "组合"
            if "TEXT_BOX" in nm:
                return "文本框"
            if "AUTO_SHAPE" in nm or "AUTO" in nm:
                return "形状"
    except Exception:
        pass
    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        return "文本框"
    return "形状"


def _shape_text(shape, limit=60):
    try:
        if shape.has_text_frame:
            t = " / ".join(x.strip() for x in shape.text_frame.text.split("\n")
                           if x.strip())
            return t[:limit]
    except Exception:
        pass
    try:
        if shape.has_table:
            tb = shape.table
            return "%d行×%d列" % (len(tb.rows), len(tb.columns))
    except Exception:
        pass
    return ""


def inspect_pptx(path, limit=4000):
    """列出 PPT 的每一页与每一个元素（编号供 edit 使用）。"""
    from pptx import Presentation
    prs = Presentation(path)
    out = ["【PPT】%s —— 共 %d 页" % (os.path.basename(path), len(prs.slides))]
    for i, slide in enumerate(prs.slides, 1):
        shps = _pptx_shapes(slide)
        out.append(" 第 %d 页（%d 个元素）" % (i, len(shps)))
        for j, sh in enumerate(shps):
            pos = " @(%g,%g) %gx%g英寸" % (
                _emu2in(sh.left), _emu2in(sh.top),
                _emu2in(sh.width), _emu2in(sh.height))
            txt = _shape_text(sh)
            fh = _fill_hex(sh)
            out.append("   [%d] %s%s%s%s" % (
                j, _shape_kind(sh), pos,
                ("  「%s」" % txt) if txt else "",
                ("  填充#%s" % fh) if fh else ""))
        try:
            nt = slide.notes_slide.notes_text_frame.text.strip()
            if nt:
                out.append("   备注：「%s」" % nt[:60])
        except Exception:
            pass
        if sum(len(x) for x in out) > limit:
            out.append("  …（内容较多，已截断）")
            break
    return "\n".join(out)


def _slide_list(prs):
    return prs.slides._sldIdLst


def _delete_slide(prs, idx):
    from pptx.oxml.ns import qn
    lst = _slide_list(prs)
    items = list(lst)
    if idx < 0 or idx >= len(items):
        return False
    el = items[idx]
    rId = el.get(qn("r:id"))
    if rId:
        try:
            prs.part.drop_rel(rId)
        except Exception:
            pass
    lst.remove(el)
    return True


def _move_slide(prs, idx, to):
    lst = _slide_list(prs)
    items = list(lst)
    if idx < 0 or idx >= len(items):
        return False
    to = max(0, min(len(items) - 1, to))
    el = items[idx]
    lst.remove(el)
    lst.insert(to, el)
    return True


def _duplicate_slide(prs, idx):
    """复制一页。图片走 `add_picture` 重新落一份关系，其余直接拷 XML。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.oxml.ns import qn
    src = prs.slides[idx]
    dest = prs.slides.add_slide(src.slide_layout)
    for sh in list(dest.shapes):
        sh._element.getparent().remove(sh._element)
    for sh in src.shapes:
        try:
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                from io import BytesIO
                dest.shapes.add_picture(BytesIO(sh.image.blob), sh.left, sh.top,
                                        sh.width, sh.height)
                continue
        except Exception:
            pass
        el = copy.deepcopy(sh._element)
        # 拷过来的 XML 里可能还引用着原页的关系 ID（图片/超链接），
        # 直接留着会指向不存在的关系 → 打开报错。一律摘掉。
        for tag in ("a:blip", "a:hlinkClick"):
            for node in el.findall(".//" + qn(tag)):
                node.getparent().remove(node)
        if el.find(qn("p:pic")) is not None:      # 摘掉 blip 的图片形状已无意义
            continue
        dest.shapes._spTree.append(el)
    try:
        dest.notes_slide.notes_text_frame.text = \
            src.notes_slide.notes_text_frame.text
    except Exception:
        pass
    return True


def _find_shape(slide, sel):
    """按序号或关键词找形状。sel 可给整数下标，也可给 'title' / 文字片段。"""
    shps = _pptx_shapes(slide)
    if isinstance(sel, int):
        return shps[sel] if 0 <= sel < len(shps) else None
    s = str(sel or "").strip()
    if not s:
        return shps[0] if shps else None
    if s.isdigit():
        i = int(s)
        return shps[i] if 0 <= i < len(shps) else None
    if s.lower() in ("title", "标题"):
        for sh in shps:                    # 取最靠上的那个有文字的形状当标题
            if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
                return sh
    for sh in shps:
        if s and s in _shape_text(sh, 999):
            return sh
    return None


def _set_shape_text(shape, text):
    """把整个形状的文字换掉，**保留第一段的字体设定**。"""
    tf = shape.text_frame
    lines = str(text).split("\n")
    p0 = tf.paragraphs[0]
    if p0.runs:
        p0.runs[0].text = lines[0]
        for r in p0.runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        p0.add_run().text = lines[0]
    for extra in list(tf.paragraphs[1:]):
        extra._p.getparent().remove(extra._p)
    for line in lines[1:]:
        np = tf.add_paragraph()
        np.add_run().text = line
    return True


def _apply_run_style(run, size=None, color=None, bold=None, italic=None):
    if size:
        run.font.size = int(float(size) * 12.7 * 1000)   # pt → EMU
    if color:
        run.font.color.rgb = _hex2rgb(color)
    if bold is not None:
        run.font.bold = bool(bold)
    if italic is not None:
        run.font.italic = bool(italic)


def _hex2rgb(v):
    from pptx.dml.color import RGBColor
    s = str(v or "000000").lstrip("#")
    try:
        return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return RGBColor(0, 0, 0)


def _replace_in_slide(slide, find, repl):
    n = 0
    for sh in slide.shapes:
        try:
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if find in r.text:
                        r.text = r.text.replace(find, repl)
                        n += 1
        except Exception:
            continue
    try:
        nt = slide.notes_slide.notes_text_frame
        for p in nt.paragraphs:
            for r in p.runs:
                if find in r.text:
                    r.text = r.text.replace(find, repl)
                    n += 1
    except Exception:
        pass
    return n


def edit_pptx(path, ops, img_bases=None):
    """对已有 pptx 执行一串修改操作，返回 (ok, 说明, 警告)。"""
    from pptx import Presentation
    from pptx.util import Inches

    from . import pptx_maker as PP
    from .pptx_maker import THEMES, FONTS

    logs, warns = [], []
    try:
        prs = Presentation(path)
    except Exception as e:
        return False, "打不开这个 pptx：%s" % e, warns

    def _pct(n):
        return max(1, min(len(prs.slides), n))

    for op in (ops or []):
        if not isinstance(op, dict):
            continue
        k = str(op.get("op") or op.get("type") or "").strip().lower()
        idx0 = op.get("slide")
        try:
            if k in ("delete_slide", "删页", "删除页"):
                i = _pct(int(idx0)) - 1
                if _delete_slide(prs, i):
                    logs.append("删除第 %d 页" % (i + 1))
                else:
                    warns.append("删页失败：第 %s 页不存在" % idx0)

            elif k in ("move_slide", "移动页", "调序"):
                i = _pct(int(idx0)) - 1
                to = max(1, min(len(prs.slides), int(op.get("to") or 1))) - 1
                if _move_slide(prs, i, to):
                    logs.append("把第 %d 页移到第 %d 位" % (i + 1, to + 1))
                else:
                    warns.append("移动失败")

            elif k in ("duplicate_slide", "复制页"):
                i = _pct(int(idx0)) - 1
                if _duplicate_slide(prs, i):
                    logs.append("复制第 %d 页" % (i + 1))

            elif k in ("replace_text", "替换", "replace"):
                find = str(op.get("find") or "")
                repl = str(op.get("replace") if op.get("replace") is not None
                           else op.get("to") or "")
                if not find:
                    warns.append("replace_text 缺 find")
                    continue
                if idx0:
                    n = _replace_in_slide(prs.slides[_pct(int(idx0)) - 1], find, repl)
                else:
                    n = sum(_replace_in_slide(s, find, repl) for s in prs.slides)
                logs.append("替换「%s」→「%s」，共 %d 处" % (find, repl, n))

            elif k in ("set_text", "改文字"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                sh = _find_shape(sl, op.get("shape", 0))
                if sh is None:
                    warns.append("第 %s 页找不到元素 %s" % (idx0, op.get("shape")))
                elif _set_shape_text(sh, op.get("text") or ""):
                    logs.append("第 %d 页元素 %s 文字已改" % (int(idx0), op.get("shape")))

            elif k in ("set_text_style", "改样式"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                sh = _find_shape(sl, op.get("shape", 0))
                if sh is None or not getattr(sh, "has_text_frame", False):
                    warns.append("第 %s 页找不到可改样式的元素" % idx0)
                    continue
                for p in sh.text_frame.paragraphs:
                    al = op.get("align")
                    if al:
                        p.alignment = PP._ALIGN.get(str(al).lower(), p.alignment)
                    for r in p.runs:
                        _apply_run_style(r, op.get("size"), op.get("color"),
                                         op.get("bold"), op.get("italic"))
                logs.append("第 %d 页元素 %s 样式已改" % (int(idx0), op.get("shape")))

            elif k in ("set_bg", "改底色"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                f = sl.background.fill
                f.solid()
                f.fore_color.rgb = _hex2rgb(op.get("color") or "FFFFFF")
                logs.append("第 %d 页底色改为 #%s" % (int(idx0), op.get("color")))

            elif k in ("set_notes", "改备注"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                sl.notes_slide.notes_text_frame.text = str(op.get("text") or "")
                logs.append("第 %d 页备注已改" % int(idx0))

            elif k in ("add_text", "加文字"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                tf = PP._textbox(sl, Inches(float(op.get("x", 0.8))),
                                 Inches(float(op.get("y", 0.8))),
                                 Inches(float(op.get("w", 6))),
                                 Inches(float(op.get("h", 1))))
                p = tf.paragraphs[0]
                if op.get("align"):
                    p.alignment = PP._ALIGN.get(str(op["align"]).lower())
                r = p.add_run()
                r.text = str(op.get("text") or "")
                PP._set_font(r, float(op.get("size") or 18),
                             op.get("color") or "333333",
                             bold=bool(op.get("bold")), font=op.get("font"))
                logs.append("第 %d 页加了文字" % int(idx0))

            elif k in ("add_image", "加图片", "插图片"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                src = PP._resolve({"_bases": list(img_bases or [])},
                                  op.get("src") or "")
                if not src or not os.path.exists(src):
                    warns.append("图片找不到：%s" % op.get("src"))
                    continue
                PP._add_pic_fit(sl, src, Inches(float(op.get("x", 0.8))),
                                Inches(float(op.get("y", 0.8))),
                                Inches(float(op.get("w", 5))),
                                Inches(float(op.get("h", 4))))
                logs.append("第 %d 页加了图片" % int(idx0))

            elif k in ("add_shape", "加形状"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                kind = str(op.get("kind") or "rect").lower()
                shape = {"rect": PP.MSO_SHAPE.RECTANGLE,
                         "round": PP.MSO_SHAPE.ROUNDED_RECTANGLE,
                         "oval": PP.MSO_SHAPE.OVAL,
                         "circle": PP.MSO_SHAPE.OVAL,
                         "triangle": PP.MSO_SHAPE.ISOSCELES_TRIANGLE}.get(
                             kind, PP.MSO_SHAPE.RECTANGLE)
                PP._rect(sl, Inches(float(op.get("x", 1))),
                         Inches(float(op.get("y", 1))),
                         Inches(float(op.get("w", 2))),
                         Inches(float(op.get("h", 1))),
                         fill=op.get("fill"), line=op.get("line"), shape=shape)
                logs.append("第 %d 页加了形状(%s)" % (int(idx0), kind))

            elif k in ("delete_shape", "删元素"):
                sl = prs.slides[_pct(int(idx0)) - 1]
                sh = _find_shape(sl, op.get("shape", 0))
                if sh is None:
                    warns.append("找不到要删的元素")
                else:
                    sh._element.getparent().remove(sh._element)
                    logs.append("第 %d 页删了一个元素" % int(idx0))

            elif k in ("set_theme", "换配色"):
                name = str(op.get("theme") or "blue").lower()
                old = THEMES.get(str(op.get("from") or "").lower()) if op.get("from") else None
                new = THEMES.get(name)
                if not new:
                    warns.append("不认识的配色 %s" % name)
                    continue
                n = _recolor(prs, old, new, THEMES)
                logs.append("配色换成 %s，改了 %d 处颜色" % (name, n))

            elif k in ("add_slide", "加一页"):
                sp = dict(op.get("slide_spec") or {})
                sp.pop("slide", None)
                st = dict(sp.get("style") or {})
                for kk in ("accent", "bg", "title_color", "title_size", "body_size",
                           "decor"):
                    if sp.get(kk) is not None and st.get(kk) is None:
                        st[kk] = sp[kk]
                st.setdefault("font", FONTS["yahei"])
                st.setdefault("decor", ["page_number"])
                sp["_bases"] = list(img_bases or [])
                lay = str(sp.get("layout") or "content").lower()
                fn = PP._LAYOUTS.get(lay)
                if fn is None:
                    warns.append("加页失败：不认识的版式 %s" % lay)
                    continue
                tname = str(op.get("theme") or "").lower() or _detect_pptx_theme(
                    prs, THEMES)
                th_new = THEMES.get(tname, THEMES["blue"])
                fn(prs, dict(th_new), st, sp, len(prs.slides) + 1)
                logs.append("加了一页（版式 %s）" % lay)

            else:
                warns.append("不认识的操作「%s」" % k)
        except Exception as e:
            warns.append("操作 %s 出错：%s: %s" % (k, type(e).__name__, e))

    try:
        prs.save(path)
    except Exception as e:
        return False, "保存失败：%s" % e, warns
    return True, "；".join(logs) if logs else "没有任何改动", warns


def _detect_pptx_theme(prs, themes):
    """从已有页面里"猜"出当前用的配色 —— 新增页应该沿用，不能硬编码蓝色。

    做法：把所有背景色/填充色/文字色统计一遍，看哪套主题的颜色命中最多。
    """
    from collections import Counter
    seen = Counter()
    for slide in prs.slides:
        try:
            bg = slide.background.fill
            if bg.type == 1:
                seen[_rgb_hex(bg.fore_color).upper()] += 3
            elif bg.type == 3:
                for st in bg.gradient_stops:
                    seen[_rgb_hex(st.color).upper()] += 3
        except Exception:
            pass
        for sh in slide.shapes:
            fh = _fill_hex(sh)
            if fh:
                seen[fh.upper()] += 2
            try:
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            rc = _rgb_hex(r.font.color).upper()
                            if rc:
                                seen[rc] += 1
            except Exception:
                pass
    best, score = "blue", -1
    for name, th in themes.items():
        s = sum(seen.get(str(th.get(k) or "").upper(), 0)
                for k in ("cover_bg", "accent", "card", "bg"))
        if s > score:
            best, score = name, s
    return best


def _recolor(prs, old, new, themes):
    """把旧配色的颜色值整体换成新配色（封面底色 / 强调色 / 内页底）。"""
    if old is None:
        # 没指明旧配色：把"六套里最像封面底色"的那个找出来
        old = None
    mapping = {}
    if old:
        for k in ("cover_bg", "accent", "bg", "card", "body", "muted"):
            if old.get(k) and new.get(k):
                mapping[old[k].upper()] = new[k]
    else:
        for th in themes.values():
            for k in ("cover_bg", "accent", "card"):
                if th.get(k):
                    mapping.setdefault(th[k].upper(), new.get(k) or new["accent"])
    n = 0
    for slide in prs.slides:
        # ⚠️ 封面/引言/结尾页用的是**渐变底**（type=3），只处理纯色底（type=1）
        # 的话，换完配色会出现"内页绿了、封面还是蓝的"——实测踩到。
        try:
            bg = slide.background.fill
            if bg.type == 1:
                cur = _rgb_hex(bg.fore_color).upper()
                if cur in mapping:
                    bg.fore_color.rgb = _hex2rgb(mapping[cur])
                    n += 1
            elif bg.type == 3:
                for stop in bg.gradient_stops:
                    cur = _rgb_hex(stop.color).upper()
                    if cur in mapping:
                        stop.color.rgb = _hex2rgb(mapping[cur])
                        n += 1
        except Exception:
            pass
        for sh in slide.shapes:
            fh = _fill_hex(sh)
            if fh and fh.upper() in mapping:
                try:
                    sh.fill.fore_color.rgb = _hex2rgb(mapping[fh.upper()])
                    n += 1
                except Exception:
                    pass
            try:
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        for r in p.runs:
                            rc = _rgb_hex(r.font.color).upper()
                            if rc and rc in mapping:
                                r.font.color.rgb = _hex2rgb(mapping[rc])
                                n += 1
            except Exception:
                pass
    return n


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def _docx_paras(doc):
    """**唯一的段落遍历顺序** —— inspect 与 edit 共用，保证编号一致。

    注意：表格里的段落不在这个列表里（python-docx 的 `doc.paragraphs` 不含）。
    """
    return list(doc.paragraphs)


def _docx_style_hint(p):
    """猜一下这段是什么（给模型看的）。"""
    t = (p.text or "").strip()
    if not t:
        return "空行"
    sz = None
    for r in p.runs:
        if r.font.size:
            sz = r.font.size.pt
            break
    bits = []
    if sz:
        bits.append("%gpt" % sz)
    b = any(r.font.bold for r in p.runs if r.font.bold)
    if b:
        bits.append("粗")
    al = getattr(p.alignment, "name", None)
    if al and al != "JUSTIFY":
        bits.append(str(al).lower())
    if p.paragraph_format.left_indent:
        bits.append("缩进")
    hint = ("[%s]" % ",".join(bits)) if bits else ""
    return "%s%s" % (("标题" if b and sz and sz >= 14 else "正文"), hint)


def inspect_docx(path, limit=4000):
    from docx import Document
    doc = Document(path)
    paras = _docx_paras(doc)
    out = ["【Word】%s —— 段落 %d 个，表格 %d 个"
           % (os.path.basename(path), len(paras), len(doc.tables))]
    for i, p in enumerate(paras):
        t = (p.text or "").strip()
        out.append(" [%d] %s%s" % (i, ("「%s」" % t[:70]) if t else "（空）",
                                   ("  " + _docx_style_hint(p)) if t else ""))
        if sum(len(x) for x in out) > limit:
            out.append(" …（内容较多，已截断）")
            break
    for ti, tb in enumerate(doc.tables):
        head = " | ".join(c.text.strip()[:12] for c in tb.rows[0].cells) \
            if len(tb.rows) else ""
        out.append(" 表 %d：%d行×%d列  表头：%s"
                   % (ti + 1, len(tb.rows), len(tb.columns), head))
    return "\n".join(out)


def _detect_docx_theme(doc, themes):
    """猜出已有 Word 用的配色 / 字体 —— 追加内容时沿用，不能硬编码。"""
    from collections import Counter

    from . import docx_maker as DM
    colors, fonts = Counter(), Counter()
    for p in doc.paragraphs:
        for r in p.runs:
            c = _rgb_hex(r.font.color).upper()
            if c:
                colors[c] += 1
            if r.font.name:
                fonts[r.font.name] += 1
    for tb in doc.tables:
        for row in tb.rows:
            for cell in row.cells:
                try:
                    if cell._tc.tcPr is not None:
                        shd = cell._tc.tcPr.find(
                            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd")
                        if shd is not None and shd.get(
                                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill"):
                            colors[shd.get(
                                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill").upper()] += 5
                except Exception:
                    pass
    best, score = "blue", -1
    for name, th in themes.items():
        s = sum(colors.get(str(th.get(k) or "").upper(), 0)
                for k in ("head", "accent", "accent2"))
        if s > score:
            best, score = name, s
    fname = "yahei"
    if fonts:
        top = fonts.most_common(1)[0][0]
        for key, pair in DM.FONT_SETS.items():
            if top in pair:
                fname = key
                break
    return best, fname


def _replace_in_doc(doc, find, repl):
    n = 0
    for p in doc.paragraphs:
        for r in p.runs:
            if find in r.text:
                r.text = r.text.replace(find, repl)
                n += 1
    for tb in doc.tables:
        for row in tb.rows:
            for c in row.cells:
                for p in c.paragraphs:
                    for r in p.runs:
                        if find in r.text:
                            r.text = r.text.replace(find, repl)
                            n += 1
    return n


def edit_docx(path, ops, img_bases=None):
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    from . import docx_maker as DM

    logs, warns = [], []
    try:
        doc = Document(path)
    except Exception as e:
        return False, "打不开这个 docx：%s" % e, warns

    ctx = None

    def _ctx():
        nonlocal ctx
        if ctx is None:
            th_name, f_name = _detect_docx_theme(doc, DM.THEMES)
            ctx = DM.make_ctx(th_name, f_name, img_bases)
        return ctx

    for op in (ops or []):
        if not isinstance(op, dict):
            continue
        k = str(op.get("op") or op.get("type") or "").strip().lower()
        try:
            if k in ("replace_text", "替换", "replace"):
                find = str(op.get("find") or "")
                repl = str(op.get("replace") if op.get("replace") is not None
                           else op.get("to") or "")
                if not find:
                    warns.append("replace_text 缺 find")
                    continue
                logs.append("替换「%s」→「%s」，共 %d 处"
                            % (find, repl, _replace_in_doc(doc, find, repl)))

            elif k in ("set_para", "改段落"):
                paras = _docx_paras(doc)
                i = int(op.get("index") or 0)
                if not (0 <= i < len(paras)):
                    warns.append("段落 %d 不存在" % i)
                    continue
                p = paras[i]
                txt = str(op.get("text") or "")
                if p.runs:
                    p.runs[0].text = txt
                    for r in p.runs[1:]:
                        r._r.getparent().remove(r._r)
                else:
                    p.add_run().text = txt
                logs.append("第 %d 段文字已改" % i)

            elif k in ("set_para_style", "改段落样式"):
                paras = _docx_paras(doc)
                i = int(op.get("index") or 0)
                if not (0 <= i < len(paras)):
                    warns.append("段落 %d 不存在" % i)
                    continue
                p = paras[i]
                if op.get("align"):
                    p.alignment = DM._ALIGN.get(str(op["align"]).lower(),
                                                WD_ALIGN_PARAGRAPH.LEFT)
                if op.get("size"):
                    p.paragraph_format.space_before = p.paragraph_format.space_before
                for r in p.runs:
                    if op.get("size"):
                        r.font.size = Pt(float(op["size"]))
                    if op.get("color"):
                        r.font.color.rgb = DM._rgb(op["color"])
                    if op.get("bold") is not None:
                        r.font.bold = bool(op["bold"])
                    if op.get("italic") is not None:
                        r.font.italic = bool(op["italic"])
                    if op.get("font"):
                        DM._set_font(r, float(op.get("size") or
                                              (r.font.size.pt if r.font.size else 11)),
                                     op.get("color") or "262626", font=op["font"])
                logs.append("第 %d 段样式已改" % i)

            elif k in ("delete_para", "删段落"):
                paras = _docx_paras(doc)
                i = int(op.get("index") or 0)
                if not (0 <= i < len(paras)):
                    warns.append("段落 %d 不存在" % i)
                    continue
                el = paras[i]._p
                el.getparent().remove(el)
                logs.append("删了第 %d 段" % i)

            elif k in ("insert_para", "插入段落"):
                paras = _docx_paras(doc)
                i = int(op.get("after") if op.get("after") is not None
                        else len(paras) - 1)
                block = dict(op.get("block") or {})
                if not block:
                    block = {"type": "para", "text": str(op.get("text") or "")}
                elif not block.get("type"):
                    block["type"] = "para"
                tmp = Document()      # 借一张白纸把块渲染出来，再搬过去
                DM.add_block(tmp, block, _ctx())
                anchor = paras[i] if 0 <= i < len(paras) else None
                if anchor is None:
                    warns.append("插入位置 %d 不存在" % i)
                    continue
                ref = anchor._p
                for el in list(tmp.element.body):
                    tag = el.tag.split("}")[-1]
                    if tag in ("p", "tbl"):
                        ref.addnext(el)
                        ref = el
                logs.append("在第 %d 段后插入了内容" % i)

            elif k in ("append", "追加"):
                block = dict(op.get("block") or {})
                if not block:
                    block = {"type": "para", "text": str(op.get("text") or "")}
                DM.add_block(doc, block, _ctx())
                logs.append("追加了内容")

            elif k in ("set_header", "改页眉"):
                for sec in doc.sections:
                    hp = sec.header.paragraphs[0]
                    for r in list(hp.runs):
                        r._r.getparent().remove(r._r)
                    hp.add_run().text = str(op.get("text") or "")
                logs.append("页眉已改")

            elif k in ("set_footer", "改页脚"):
                for sec in doc.sections:
                    fp = sec.footer.paragraphs[0]
                    for r in list(fp.runs):
                        r._r.getparent().remove(r._r)
                    DM._add_field(fp, "PAGE", "1", 9, "808080")
                logs.append("页脚已重设为页码")

            elif k in ("delete_table", "删表格"):
                ti = int(op.get("table") or 1) - 1
                if not (0 <= ti < len(doc.tables)):
                    warns.append("表格 %s 不存在" % op.get("table"))
                    continue
                el = doc.tables[ti]._tbl
                el.getparent().remove(el)
                logs.append("删了第 %d 个表格" % (ti + 1))

            else:
                warns.append("不认识的操作「%s」" % k)
        except Exception as e:
            warns.append("操作 %s 出错：%s: %s" % (k, type(e).__name__, e))

    if ctx and ctx["warnings"]:
        warns.extend(ctx["warnings"])
    try:
        doc.save(path)
    except Exception as e:
        return False, "保存失败：%s" % e, warns
    return True, "；".join(logs) if logs else "没有任何改动", warns


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def inspect(path):
    """按扩展名分派：看结构。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pptx":
        return inspect_pptx(path)
    if ext == ".docx":
        return inspect_docx(path)
    return "只支持 .docx 和 .pptx，这个文件是 %s" % ext


def edit(path, ops, img_bases=None):
    """按扩展名分派：执行修改。返回 (ok, 说明, 警告)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pptx":
        return edit_pptx(path, ops, img_bases)
    if ext == ".docx":
        return edit_docx(path, ops, img_bases)
    return False, "只支持 .docx 和 .pptx，这个文件是 %s" % ext, []
