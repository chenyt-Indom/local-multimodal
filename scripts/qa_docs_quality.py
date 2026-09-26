# -*- coding: utf-8 -*-
"""逐项检查生成的 Word / PPT / Excel 质量。

不只看"文件存在"，而是解析内部结构：样式有没有真生效、中文有没有乱码、
表格公式对不对、PPT 的加粗/高亮有没有渲染出来。
"""
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

import os as _os
# 从脚本位置往上找到仓库根，再进 data/library —— 换机器/换绝对路径不用改代码
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
LIB = _os.path.join(_ROOT, "data", "library")
DOCX = LIB + r"\中小企业数字化转型调研报告.docx"
PPTX = LIB + r"\2026 年第三季度项目汇报.pptx"
XLSX = LIB + r"\研发中心 2026 年三季度费用明细.xlsx"

PASS, FAIL = 0, 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s  %s" % (name, str(detail)[:70]))
    else:
        FAIL += 1
        print("  [!!] %s  %s" % (name, str(detail)[:70]))


# ================= Word =================
print("=" * 62)
print("【1】Word 质量检查")
print("=" * 62)
try:
    from docx import Document
    d = Document(DOCX)
    paras = d.paragraphs
    txt = "\n".join(p.text for p in paras)
    styles = {}
    for p in paras:
        styles[p.style.name] = styles.get(p.style.name, 0) + 1
    print("  段落数 %d，样式分布：%s" % (len(paras), dict(list(styles.items())[:6])))
    ck("能正常打开且非空", len(paras) > 5, "%d 段落" % len(paras))
    ck("标题文字正确", "中小企业数字化转型调研报告" in txt)
    ck("中文无乱码", "数字化转型" in txt and "????" not in txt)
    ck("分节标题都在", all(k in txt for k in ("调研背景", "主要发现", "原因分析", "对策建议", "结论")))
    # ⚠️ 列表检查以前写的是 `"List" in styles` —— **断言本身是错的**：
    #    本项目的列表刻意**不用** Word 原生列表样式（`List Paragraph`），
    #    而是用"手工项目符号 + 悬挂缩进"实现（见 docx_maker._render_list），
    #    目的是让项目符号能跟随主题色。原生列表的项目符号颜色由 numbering.xml 定，
    #    主题色会被丢掉。所以要看的是**符号本身在不在**，不是样式名。
    bullets = sum(1 for p in paras if p.text.strip().startswith("• "))
    ordered = sum(1 for p in paras if p.text.strip()[:2] in ("1.", "2.", "3."))
    ck("有项目符号列表（•）", bullets >= 3, "• 开头段落 %d 个" % bullets)
    ck("有有序列表（1. 2. 3.）", ordered >= 3, "数字编号段落 %d 个" % ordered)
    ck("加粗（**…**）已渲染成真加粗，未把星号印出来",
       "**" not in txt, "正文里 '**' 出现 %d 次" % txt.count("**"))
    # 检查是否真有加粗 run
    bold_runs = [r.text for p in paras for r in p.runs if r.bold and r.text.strip()]
    ck("存在加粗文本", len(bold_runs) > 0, "如：%s" % (bold_runs[0][:40] if bold_runs else "无"))
    # 表格 / 目录
    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        doc_xml = z.read("word/document.xml").decode("utf-8", "ignore")
    ck("目录域已保留 TOC 结构或标题层级",
       "TOC" in doc_xml or "Heading1" in "".join(styles.keys()) or any("eading" in s for s in styles),
       "样式：%s" % [s for s in styles if "eading" in s or "TOC" in s][:4])
except Exception as e:
    ck("Word 解析", False, str(e)[:90])

# ================= PPT =================
print()
print("=" * 62)
print("【2】PPT 质量检查")
print("=" * 62)
try:
    from pptx import Presentation
    p = Presentation(PPTX)
    slides = list(p.slides)
    print("  幻灯片 %d 页，尺寸 %.0f×%.0f" % (len(slides), p.slide_width, p.slide_height))
    ck("页数正确（7~8 页）", 6 <= len(slides) <= 9, "%d 页" % len(slides))

    all_txt, bold_cnt, hl_cnt = [], 0, 0
    for s in slides:
        for sh in s.shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    for r in para.runs:
                        all_txt.append(r.text)
                        if r.font.bold:
                            bold_cnt += 1
                        # 高亮：pptx 里用 highlight 或底纹色
                        try:
                            if r.font.highlight is not None:
                                hl_cnt += 1
                        except Exception:
                            pass
    joined = "".join(all_txt)
    ck("中文无乱码", "第三季度" in joined and "??" not in joined)
    ck("封面标题在", "2026 年第三季度项目汇报" in joined)
    ck("各页标题在", all(k in joined for k in ("本季概览", "关键成果", "问题与风险", "下季重点")))
    ck("**加粗** 语法已渲染（未印出星号）", "**" not in joined, "原文含 '**' %d 次" % joined.count("**"))
    ck("存在真加粗 run", bold_cnt > 0, "%d 处" % bold_cnt)
    ck("==高亮== 语法已渲染（未印出等号标记）", "==" not in joined, "原文含 '==' %d 次" % joined.count("=="))
    # ⚠️ 高亮检查以前读 `r.font.highlight` —— **python-pptx 根本没这个属性**，
    #    永远取到 None，于是把"确实生效了的高亮"报成 0 处（假失败）。
    #    正确做法是直接数幻灯片 XML 里的 `<a:highlight>` 节点。
    hl_cnt = 0
    with zipfile.ZipFile(PPTX) as z:
        for nm in z.namelist():
            if nm.startswith("ppt/slides/slide") and nm.endswith(".xml"):
                hl_cnt += z.read(nm).decode("utf-8", "ignore").count("<a:highlight>")
    ck("高亮已应用（a:highlight）", hl_cnt > 0, "%d 处" % hl_cnt)
    ck("双栏版式页有两栏文字",
       any(("技术侧" in t or "协作侧" in t) for t in all_txt))
    ck("结尾页在", "感谢聆听" in joined)
except Exception as e:
    ck("PPT 解析", False, str(e)[:90])

# ================= Excel =================
print()
print("=" * 62)
print("【3】Excel 质量检查")
print("=" * 62)
try:
    from openpyxl import load_workbook
    wb = load_workbook(XLSX)
    ws = wb.active
    print("  工作表：%s，范围 %s" % (wb.sheetnames, ws.dimensions))
    rows = list(ws.iter_rows(values_only=True))
    ck("工作表名正确", "费用明细" in wb.sheetnames, wb.sheetnames)
    ck("表头正确",
       any(isinstance(r[0], str) and r[0] == "部门" for r in rows if r),
       [c.value for c in ws[1]][:6] if ws.max_row else "")
    data_rows = [r for r in rows[2:] if r and isinstance(r[0], str)
                 and r[0] != "部门" and "合计" not in str(r[0])]
    ck("数据行数 8", len(data_rows) == 8, "%d 行" % len(data_rows))
    ck("中文无乱码", any("研发一部" in str(r) for r in rows if r))
    # 数字是数字类型，不是字符串
    num_ok = 0
    for r in rows:
        for v in r:
            if isinstance(v, (int, float)):
                num_ok += 1
    ck("数字以数值类型存储（不是文本）", num_ok >= 20, "%d 个数值单元格" % num_ok)
    ck("有合计行", any("合计" in str(c) for r in rows for c in r if c))
    # 合计是否**用真公式**、且覆盖范围正好是明细行
    # ⚠️ 以前这里读"合计行的数值"，但 openpyxl 默认（非 data_only）读到的是
    #    **公式字符串** `=SUM(C3:C10)`，转成数值列表是空的 → 又把对的报成错的。
    #    真正该验的是：① 是不是公式（不是写死的数字）；② 范围对不对得上行号。
    tot_idx = next((i for i, r in enumerate(ws.iter_rows(min_row=1), start=1)
                    if any(isinstance(c.value, str) and "合计" in c.value for c in r)), None)
    if tot_idx:
        exp_last = tot_idx - 1
        col_c = ws.cell(row=tot_idx, column=3).value
        col_d = ws.cell(row=tot_idx, column=4).value
        print("    合计行 %d 的公式：" % tot_idx, repr(col_c), repr(col_d))
        want_c = "=SUM(C3:C%d)" % exp_last
        ck("合计是活公式而非死值（改明细会自动重算）",
           isinstance(col_c, str) and col_c.startswith("="),
           repr(col_c))
        ck("合计公式范围正好覆盖明细行",
           str(col_c).replace(" ", "").upper() == want_c.upper(),
           "期望 %s" % want_c)
        # 顺带真算一遍，确认明细数值本身没问题（改公式/数据后能被 Excel 算对）
        got = sum(ws.cell(row=r, column=3).value or 0
                  for r in range(3, exp_last + 1)
                  if isinstance(ws.cell(row=r, column=3).value, (int, float)))
        print("    明细预算列求和 = %s" % got)
        ck("明细求和结果合理", got > 0, "%s" % got)
    ck("冻结首行（便于滚动查看）", ws.freeze_panes is not None, ws.freeze_panes)
    # ⚠️ 表头在第 2 行（第 1 行是标题）。以前查的是第 1 行 → 标题行本来就无底色，
    #    于是把"确实有蓝色表头"报成"没有填充色"（假失败）。
    hdr = [c for c in ws[2] if c.value is not None]
    hdr_fill = [c for c in hdr
                if c.fill and c.fill.fgColor and str(c.fill.fgColor.rgb) not in ("None", "00000000")]
    ck("表头有填充色（第 2 行）", len(hdr_fill) == len(hdr) and hdr,
       "%d/%d 个表头单元格有底色 %s"
       % (len(hdr_fill), len(hdr), str(hdr_fill[0].fill.fgColor.rgb) if hdr_fill else ""))
    ck("表头加粗", all(c.font.bold for c in hdr) and hdr, "")
except Exception as e:
    ck("Excel 解析", False, str(e)[:90])

print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
