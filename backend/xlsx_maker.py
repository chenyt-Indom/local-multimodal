# -*- coding: utf-8 -*-
"""把结构化数据排成一份**好看的 .xlsx**（Excel / WPS / Numbers 都能打开）。

设计原则跟 pptx_maker / docx_maker 一致：**模型只填数据，不写代码**。
用 xlsxwriter（python-pptx 的依赖里本来就带它，不用额外装包）。

一个工作表的样子：
    合并大标题
    ┌────┬──────┬────┐
    │ 表头（主题色底 + 白字 + 居中 + 冻结）│
    ├────┼──────┼────┤   ← 隔行浅底色
    │ 数据 ...        │   ← 可自动加合计行 / 条件格式 / 图表
    └────┴──────┴────┘
    小字备注（数据来源…）
"""
import os
import re

import xlsxwriter

DEFAULT_THEME = "blue"

# 六套配色：和 PPT / Word 那套对齐（表头底色 + 强调色 + 隔行底色）
THEMES = {
    "blue":   {"head": "#2E75B6", "accent": "#2E75B6", "zebra": "#EAF1F9", "line": "#BDD3E8"},
    "green":  {"head": "#2F8F5B", "accent": "#2F8F5B", "zebra": "#EAF6EF", "line": "#BFE0CC"},
    "warm":   {"head": "#C87F2E", "accent": "#C87F2E", "zebra": "#FDF3E7", "line": "#EBD4B4"},
    "purple": {"head": "#6B4FBB", "accent": "#6B4FBB", "zebra": "#F0EDFA", "line": "#CFC5EC"},
    "mono":   {"head": "#404040", "accent": "#404040", "zebra": "#F2F2F2", "line": "#CFCFCF"},
    "red":    {"head": "#B3382C", "accent": "#B3382C", "zebra": "#FBEDEB", "line": "#E6C3BD"},
}

# 列格式（模型写这些关键字，也可以直接写 Excel 的格式串）
NUM_FORMATS = {
    "text": "General", "str": "General", "general": "General",
    "int": "#,##0", "integer": "#,##0", "number": "#,##0.00", "num": "#,##0.00",
    "money": '¥#,##0.00', "currency": '¥#,##0.00', "cny": '¥#,##0.00',
    "percent": "0.0%", "pct": "0.0%",
    "score": "0.00", "sci": "0.00E+00",
    "date": "yyyy-mm-dd", "datetime": "yyyy-mm-dd hh:mm", "time": "hh:mm",
}

_ILLEGAL_SHEET = re.compile(r"[\[\]:*?/\\]")


def _num_fmt(v):
    """把 'money' / '0.00' / None 统一成 xlsxwriter 能用的 format 串。"""
    s = str(v or "").strip()
    if not s:
        return "General"
    return NUM_FORMATS.get(s.lower(), s)      # 不是关键字就当成原始格式串


def _disp_text(v, nf) -> str:
    """按数字格式估算「Excel 里实际会显示的文本」，**专供算列宽用**。

    ⚠️ 2026-09-26 修（实测导出 PDF 才看出来）：列宽原来只按**原始值**估 ——
    `256000` 算 6 个字符 → 列宽 9，可套上货币格式后实际显示是 `¥256,000.00`
    （11 个字符）→ 列不够宽，单元格直接显示 **`#########`**。
    用户打开表格看到的就是一堆井号，等于数据"看不见"。
    """
    s = str("" if v is None else v)
    if not nf or nf == "General":
        return s
    # ⚠️ 必须先用 _is_num 判：`_as_num` 对认不出的值会**静默返回 0**
    #    （实测 `_as_num("2026-03-01") == 0`），日期列会被算成 1 个字符宽 → 列太窄。
    if not _is_num(v):
        return s
    n = _as_num(v)
    if n is None:
        return s
    dec = 0
    if "." in nf:                       # 小数位数 = 小数点后 0/# 的个数
        dec = sum(1 for ch in nf.split(".", 1)[1] if ch in "0#")
    pct = "%" in nf
    if pct:
        n = n * 100
    body = ("{:,.%df}" % dec).format(n) if "," in nf else ("{:.%df}" % dec).format(n)
    prefix = ""
    for sym in ("¥", "￥", "$", "€", "£"):
        if sym in nf:
            prefix = sym
            break
    return prefix + body + ("%" if pct else "")


def _is_num(v):
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return True
    s = str(v).strip().replace(",", "").replace("¥", "").replace("%", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _as_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    s = str(v).strip().replace(",", "").replace("¥", "")
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return 0
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return 0


def _safe_name(s, fallback):
    s = _ILLEGAL_SHEET.sub("", str(s or "").strip())[:28] or fallback
    return s


def build_xlsx(path, sheets, theme=DEFAULT_THEME, author="本地多模态助手",
               colors=None):
    """按 sheets 规格生成 xlsx。返回 {"ok","sheets","rows","warnings"}。"""
    warnings = []
    # 支持自定义配色（见 pptx_maker.apply_colors 的说明）
    from backend.pptx_maker import apply_colors
    th = apply_colors(
        THEMES.get(str(theme or "").strip().lower(), THEMES[DEFAULT_THEME]), colors)
    if not isinstance(sheets, list) or not sheets:
        return {"ok": False, "error": "至少要有一个工作表（sheets）"}

    out = str(path or "")
    if not out.lower().endswith(".xlsx"):
        out += ".xlsx"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    wb = xlsxwriter.Workbook(out, {"in_memory": True})
    try:
        wb.set_properties({"author": author, "comments": "由本地多模态助手生成"})

        # ---------------- 共用格式 ----------------
        f_title = wb.add_format({"bold": True, "font_size": 15, "font_color": "#1F2A37",
                                 "align": "left", "valign": "vcenter"})
        f_sub = wb.add_format({"font_size": 10, "font_color": "#6B7280", "align": "left"})
        f_head = wb.add_format({"bold": True, "font_size": 11, "font_color": "#FFFFFF",
                                "bg_color": th["head"], "align": "center",
                                "valign": "vcenter", "border": 1,
                                "border_color": th["head"], "text_wrap": True})
        f_cell = wb.add_format({"font_size": 11, "valign": "vcenter",
                                "border": 1, "border_color": th["line"]})
        f_zebra = wb.add_format({"font_size": 11, "valign": "vcenter", "bg_color": th["zebra"],
                                 "border": 1, "border_color": th["line"]})
        f_total = wb.add_format({"bold": True, "font_size": 11, "bg_color": th["zebra"],
                                 "top": 2, "top_color": th["head"],
                                 "border": 1, "border_color": th["line"]})
        f_note = wb.add_format({"font_size": 9.5, "font_color": "#6B7280", "italic": True})

        made, total_rows = 0, 0
        used_names = set()

        for si, sh in enumerate(sheets):
            if not isinstance(sh, dict):
                continue
            name = _safe_name(sh.get("name"), "Sheet%d" % (si + 1))
            while name in used_names:
                name = name[:26] + "_2"
            used_names.add(name)
            ws = wb.add_worksheet(name)

            header = [str(x) for x in (sh.get("header") or sh.get("headers") or [])]
            rows = sh.get("rows") or []
            ncol = max([len(header)] + [len(r) for r in rows if isinstance(r, (list, tuple))] or [0])
            if not ncol:
                ws.write(0, 0, "（这张表没有数据）", f_sub)
                made += 1
                continue

            formats = sh.get("formats") or sh.get("number_formats") or []
            widths = sh.get("widths") or sh.get("col_widths") or []
            title = str(sh.get("title") or "").strip()
            note = str(sh.get("note") or sh.get("caption") or "").strip()

            r = 0
            if title:                                  # 大标题（跨列合并）
                ws.merge_range(r, 0, r, max(0, ncol - 1), title, f_title)
                ws.set_row(r, 30)
                r += 1
            head_row = r
            if header:
                for c in range(ncol):
                    ws.write(head_row, c, header[c] if c < len(header) else "", f_head)
                ws.set_row(head_row, 22)
                r += 1

            body_start = r
            nrow = 0
            for data in rows:
                if not isinstance(data, (list, tuple)):
                    continue
                for c in range(ncol):
                    v = data[c] if c < len(data) else ""
                    fmt = f_zebra if (sh.get("zebra", True) and nrow % 2 == 1) else f_cell
                    cf = dict(fmt.__dict__) if False else None   # 占位，保持简单
                    nf = _num_fmt(formats[c] if c < len(formats) else "")
                    cellf = fmt
                    if nf != "General" and _is_num(v):
                        cellf = wb.add_format({"font_size": 11, "valign": "vcenter",
                                               "border": 1, "border_color": th["line"],
                                               "num_format": nf,
                                               "align": "right",
                                               "bg_color": th["zebra"] if (sh.get("zebra", True) and nrow % 2 == 1) else "#FFFFFF"})
                        ws.write_number(r, c, _as_num(v), cellf)
                    elif _is_num(v) and isinstance(v, (int, float)):
                        ws.write_number(r, c, v, fmt)
                    else:
                        ws.write(r, c, "" if v is None else str(v), fmt)
                nrow += 1
                r += 1
            total_rows += nrow

            # 合计行：只在「有数字列」且用户没自己写合计时才加
            if sh.get("total_row") or sh.get("totals"):
                label = str(sh.get("total_label") or "合计")
                # ⚠️ 不是所有数字列都该求和 —— 把「单价 199.5 + 349 + …」加起来毫无意义
                #    （实测导出后合计行出现 2047 这种怪数字）。模型可以显式给
                #    total_cols；没给就按列名跳过明显是"单价/比率"的列。
                skip_names = ("单价", "均价", "价格", "比率", "占比", "百分比", "比例",
                              "折扣", "price", "rate", "ratio", "percent")
                cols = sh.get("total_cols")
                if cols:
                    want_cols = {int(c) for c in cols}
                else:
                    want_cols = set()
                    for c in range(1, ncol):
                        head = header[c] if c < len(header) else ""
                        if any(k in str(head).lower() for k in skip_names):
                            continue
                        want_cols.add(c)
                ws.write(r, 0, label, f_total)
                _tf_cache = {}          # 每个数字格式只建一次 format 对象
                for c in range(1, ncol):
                    vals = [x[c] for x in rows
                            if isinstance(x, (list, tuple)) and c < len(x) and _is_num(x[c])]
                    if c in want_cols and vals and len(vals) == nrow:
                        col_letter = (chr(65 + c) if c < 26
                                      else chr(64 + c // 26) + chr(65 + c % 26))
                        # 合计数字也要**跟着该列的格式走** —— 否则金额列合计显示成
                        # 光秃秃的 442000，和上面带 ¥ 的明细不一致（实测看到的）。
                        _nf = _num_fmt(formats[c] if c < len(formats) else "")
                        tf = _tf_cache.get(_nf)
                        if tf is None:
                            if _nf == "General":
                                tf = f_total
                            else:
                                tf = wb.add_format({"bold": True, "font_size": 11,
                                                   "bg_color": th["zebra"],
                                                   "top": 2, "top_color": th["head"],
                                                   "border": 1, "border_color": th["line"],
                                                   "num_format": _nf, "align": "right"})
                            _tf_cache[_nf] = tf
                        ws.write_formula(
                            r, c, "=SUM(%s%d:%s%d)" % (col_letter, body_start + 1,
                                                       col_letter, body_start + nrow),
                            tf, sum(_as_num(v) for v in vals))
                    else:
                        ws.write(r, c, "", f_total)
                ws.set_row(r, 20)
                r += 1

            if note:
                r += 1
                ws.merge_range(r, 0, r, max(0, ncol - 1), note, f_note)

            # 列宽：给了就用，没给按内容估
            for c in range(ncol):
                if c < len(widths) and widths[c]:
                    w = float(widths[c])
                else:
                    _nf = _num_fmt(formats[c] if c < len(formats) else "")
                    cells = [header[c] if c < len(header) else ""] + \
                            [_disp_text(x[c] if c < len(x) else "", _nf) for x in rows
                             if isinstance(x, (list, tuple))]
                    # 中文按 2 个字符宽算（原来这里先按 len() 算了一遍又被覆盖，
                    # 是死代码 —— 顺手删掉，免得读的人以为有两套口径）
                    longest = max([sum(2 if ord(ch) > 127 else 1 for ch in str(x))
                                   for x in cells] or [8])
                    w = min(42, max(9, longest + 3))
                ws.set_column(c, c, w)

            if sh.get("freeze", True) and header:
                ws.freeze_panes(head_row + 1, 0)
            if sh.get("autofilter", True) and header and nrow:
                ws.autofilter(head_row, 0, body_start + nrow - 1, ncol - 1)

            # 打印友好：宽表压成一页宽，必要时横向 —— 不然打印/导 PDF 会截断
            if sh.get("print_fit", True):
                try:
                    ws.fit_to_pages(1, 0)
                    if ncol >= 6 or isinstance(sh.get("chart"), dict):
                        ws.set_landscape()
                    ws.set_paper(9)                      # A4
                    ws.set_margins(0.4, 0.4, 0.5, 0.4)
                    if header:
                        ws.repeat_rows(head_row)         # 每页重复表头
                except Exception:
                    pass

            # 条件格式（比如给金额列做色阶）—— 直接给列号就行
            cond = sh.get("conditional") or sh.get("color_scale")
            if cond:
                if isinstance(cond, int):
                    cond = {"col": cond, "type": "3_color_scale"}
                c0 = int(cond.get("col") or 0)
                kind = str(cond.get("type") or "3_color_scale")
                rng = "%s%d:%s%d" % (chr(65 + c0) if c0 < 26 else "A", body_start + 1,
                                     chr(65 + c0) if c0 < 26 else "A", body_start + nrow)
                try:
                    if kind in ("3_color_scale", "color_scale"):
                        ws.conditional_format(rng, {"type": "3_color_scale"})
                    elif kind == "data_bar":
                        ws.conditional_format(rng, {"type": "data_bar"})
                    elif kind == "duplicate":
                        ws.conditional_format(rng, {"type": "duplicate"})
                    elif kind == "cell" and cond.get("criteria"):
                        ws.conditional_format(rng, {"type": "cell",
                                                    "criteria": cond["criteria"],
                                                    "value": cond.get("value", 0),
                                                    "format": wb.add_format(
                                                        {"bg_color": "#FFC7CE",
                                                         "font_color": "#9C0006"})})
                except Exception as e:
                    warnings.append("条件格式没做成：%s" % e)

            # 图表
            ch = sh.get("chart")
            if isinstance(ch, dict) and nrow:
                try:
                    kind = str(ch.get("kind") or "column").lower()
                    kind = {"bar": "column", "column": "column", "柱状": "column",
                            "line": "line", "折线": "line", "pie": "pie", "饼图": "pie",
                            "area": "area", "doughnut": "doughnut",
                            "圆环": "doughnut"}.get(kind, "column")
                    chart = wb.add_chart({"type": kind})
                    cat_col = int(ch.get("categories_col") or 0)
                    cols = ch.get("value_cols")
                    if not cols:
                        cols = [c for c in range(ncol) if c != cat_col][: 3]
                    for c in cols:
                        c = int(c)
                        if c >= ncol:
                            continue
                        # 用**单元格引用**而不是值：以后改数据，图会跟着变
                        col_letter = chr(65 + c) if c < 26 else chr(64 + c // 26) + chr(65 + c % 26)
                        cat_letter = (chr(65 + cat_col) if cat_col < 26
                                      else chr(64 + cat_col // 26) + chr(65 + cat_col % 26))
                        chart.add_series({
                            "name": header[c] if c < len(header) else "列%d" % (c + 1),
                            "categories": "='%s'!$%s$%d:$%s$%d" % (
                                name, cat_letter, body_start + 1, cat_letter, body_start + nrow),
                            "values": "='%s'!$%s$%d:$%s$%d" % (
                                name, col_letter, body_start + 1, col_letter, body_start + nrow),
                        })
                    chart.set_title({"name": str(ch.get("title") or "")})
                    chart.set_style(int(ch.get("style") or 10))
                    if kind != "pie" and kind != "doughnut":
                        chart.set_x_axis({"name": str(ch.get("x") or "")})
                        chart.set_y_axis({"name": str(ch.get("y") or "")})
                    chart.set_size({"width": int(ch.get("width") or 520),
                                    "height": int(ch.get("height") or 320)})
                    # 默认放在**表格下方**：放右侧容易被打印页宽截断（实测踩到，
                    # 导成 PDF 只剩图表左半边，看着像"只画了一根柱子"）。
                    anchor = str(ch.get("position") or ("A%d" % (r + 2)))
                    ws.insert_chart(anchor, chart)
                except Exception as e:
                    warnings.append("图表没做成：%s" % e)

            made += 1

        wb.close()
    except Exception as e:
        try:
            wb.close()
        except Exception:
            pass
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    return {"ok": True, "path": out, "sheets": made, "rows": total_rows,
            "warnings": warnings, "theme": theme}
