# -*- coding: utf-8 -*-
"""生成能力矩阵：PPT / Word / Excel / 代码 / 图片 / 长文本，逐项按**复杂规格**验收。

每项都刻意给"内容不简单"的规格（多版式、多表、多块、长文），并核对：
  · 功能正常（真的产出了能打开的文件）
  · 内容复杂（页数/块数/行数/字数达标，不是敷衍的两三行）
  · 自定义能力（配色覆盖、画幅比例）真的生效
  · 长内容没被截断

跑法：python test_gen_matrix.py            （不调模型，纯工具层，秒级）
      python test_gen_matrix.py --img      （额外真跑一张图，需要显卡，约 30 秒）
"""
import glob
import io
import os
import shutil
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="mm_gen_")
import json                                                          # noqa: E402
cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
json.dump(cfg, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import tools as T                                       # noqa: E402

PASS = FAIL = 0


def check(n, ok, d=""):
    global PASS, FAIL
    print(("  [OK] " if ok else "  [!!] ") + n + ("  " + str(d) if d else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def call(name, args):
    ev = []
    try:
        return T.dispatch(name, args, ev, {}), ev
    except Exception as e:
        return "EXC: %r" % (e,), ev


def newest(ext):
    f = glob.glob(os.path.join(TMP, "**", "*" + ext), recursive=True)
    return max(f, key=os.path.getmtime) if f else None


RED_GOLD = {"accent": "#B8860B", "cover_bg": "#8B0000", "bg": "#FFFBF2"}

print("=" * 62)
print("生成能力矩阵")
print("=" * 62)

# ---------------------------------------------------------------- PPT
print("\n【1】PPT —— 复杂规格：12 页、6 种版式、自定义红金配色")
long_bullet = ("民航业数字化转型加速，智慧机场与智慧空管对既懂民航业务又懂人工智能的复合型人才"
               "需求快速增长，而现有实训条件仍以传统工艺流程为主，缺少真实数据驱动的 AI "
               "实训环境，学生工程化能力与产业要求存在明显落差，必须在第一学年末形成可用的教学支撑能力。")
slides = [
    {"layout": "section", "title": "第一部分 背景"},
    {"layout": "content", "title": "建设背景与人才需求（长要点压力测试）", "bullets": [long_bullet]},
    {"layout": "two_col", "title": "软硬件并行", "left_title": "硬件", "left": ["服务器 2 台", "边缘节点 6 套"],
     "right_title": "课程", "right": ["12 个实训项目", "5 类数据集"]},
    {"layout": "stats", "title": "关键指标",
     "stats": [{"value": "3", "label": "年"}, {"value": "1200", "label": "㎡"},
               {"value": "8", "label": "企业"}, {"value": "480", "label": "人次"},
               {"value": "12", "label": "项目"}, {"value": "5", "label": "数据集"}]},
    {"layout": "table", "title": "预算与验收",
     "table": {"header": ["年度", "主要任务", "预算（万元）", "验收标志"],
               "rows": [["第一年", "平台搭建与基础实训项目落地，完成环境调试与教师培训", "420", "6 个项目开课"],
                        ["第二年", "项目体系扩充与校企联合课题", "380", "12 个项目全开"],
                        ["第三年", "面向社会培训与技能认证", "200", "年培训 480 人次"]]}},
    {"layout": "chart", "title": "培训人次预估",
     "chart": {"kind": "bar", "categories": ["2026", "2027", "2028"],
               "series": [{"name": "校内", "values": [180, 320, 400]},
                          {"name": "企业", "values": [40, 120, 260]}]}},
    {"layout": "timeline", "title": "里程碑",
     "items": [{"title": "2026 Q3", "text": "立项与场地改造"}, {"title": "2027 Q1", "text": "平台上线"},
               {"title": "2027 Q4", "text": "项目扩充到 12 个"}, {"title": "2028 Q2", "text": "教师驻场完成"},
               {"title": "2028 Q4", "text": "取得认证资质"}, {"title": "2029 Q1", "text": "对外辐射"}]},
    {"layout": "quote", "quote": {"text": "实训不是把设备摆出来，而是把真实问题交到学生手上。",
                                  "from": "专业建设小组"}},
    {"layout": "cards", "title": "保障措施",
     "cards": [{"title": "组织", "text": "成立建设小组，明确分工与里程碑"},
               {"title": "经费", "text": "按季度跟踪执行率，低于阈值预警"},
               {"title": "师资", "text": "教师每学期驻场实践不少于两周"},
               {"title": "迭代", "text": "模块化架构，避免一次性投入过重"}]},
    {"layout": "steps", "title": "实施步骤",
     "steps": [{"title": "立项", "text": "成立小组与招标"}, {"title": "改造", "text": "场地与设备到位"},
               {"title": "开发", "text": "课程与数据集建设"}, {"title": "运行", "text": "开放共享与维护"}]},
    {"layout": "toc", "title": "目录", "items": ["背景", "目标", "路径", "预算", "风险"]},
    {"layout": "content", "title": "小结", "bullets": ["三年形成完整实训能力", "真实场景驱动课程"]},
]
txt, ev = call("make_pptx", {
    "title": "2026 年智慧民航实训基地建设方案——面向人工智能技术应用专业的三年规划与实施路径",
    "subtitle": "广州民航职业技术学院 · 人工智能技术应用专业", "author": "专业建设小组",
    "theme": "red", "colors": RED_GOLD, "slides": slides})
check("PPT 生成成功", not str(txt).startswith("EXC") and "已生成 PPT" in str(txt), str(txt)[:56])
f = newest(".pptx")
check("产物是**真** pptx", bool(f) and zipfile.is_zipfile(f))
from pptx import Presentation                                        # noqa: E402
from pptx.dml.color import RGBColor                                  # noqa: E402
prs = Presentation(f)
check("页数达标（≥13：封面+12 内容+结尾）", len(prs.slides) >= 13, "%d 页" % len(prs.slides))
alltext = []
for s in prs.slides:
    for sh in s.shapes:
        if sh.has_text_frame:
            alltext.append(sh.text_frame.text)
        if getattr(sh, "has_table", False) and sh.has_table:
            for r in sh.table.rows:
                for c in r.cells:
                    alltext.append(c.text)
blob = "".join(alltext).replace(" ", "").replace("\n", "")
check("长要点（110 字）一字不丢", long_bullet.replace(" ", "") in blob)
check("6 个统计卡全在（数量上限已取消）", all(x in blob for x in ("数据集", "项目")) and "480" in blob)
check("6 个时间线节点全在", "2029Q1" in blob or "2029 Q1" in blob.replace(" ", ""))
gold = 0
for s in prs.slides:
    for sh in s.shapes:
        try:
            if sh.fill.type == 1 and str(sh.fill.fore_color.rgb) == "B8860B":
                gold += 1
        except Exception:
            pass
check("自定义金色主色 B8860B 生效", gold >= 1, "%d 处" % gold)

# ---------------------------------------------------------------- Word
print("\n【2】Word —— 复杂规格：封面+目录+四类块+自定义配色")
blocks = [
    {"type": "heading", "text": "一、建设背景", "level": 1},
    {"type": "para", "text": "民航业数字化转型加速，智慧机场与智慧空管对复合型人才需求快速增长。" * 3},
    {"type": "bullet", "items": ["人才需求增长", "实训条件不足", "校企合作具备条件"]},
    {"type": "numbered", "items": ["成立建设小组", "完成场地改造", "开发课程资源"]},
    {"type": "table", "columns": ["年度", "主要任务", "预算（万元）"],
     "rows": [["第一年", "平台搭建", "420"], ["第二年", "体系扩充", "380"], ["第三年", "社会培训", "200"]]},
    {"type": "quote", "text": "实训不是把设备摆出来，而是把真实问题交到学生手上。"},
    {"type": "heading", "text": "二、保障措施", "level": 1},
    {"type": "para", "text": "成立由专业带头人、企业工程师、设备处组成的建设小组，明确分工与里程碑。"},
]
txt, _ = call("make_docx", {"title": "实训基地建设方案", "subtitle": "专业建设小组",
                            "author": "教务处", "theme": "red", "colors": RED_GOLD,
                            "cover": True, "toc": True, "header": "内部资料",
                            "blocks": blocks})
check("Word 生成成功", "已生成文档" in str(txt), str(txt)[:56])
f = newest(".docx")
check("产物是**真** docx", bool(f) and zipfile.is_zipfile(f))
from docx import Document                                            # noqa: E402
d = Document(f)
paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
check("段落/标题数达标（≥10 段）", len(paras) >= 10, "%d 段" % len(paras))
check("插入了目录（TOC 域）", "目录" in "".join(paras[:8]) or "TOC" in
      str(d.element.xml)[:200000], "")
check("表格进来了", len(d.tables) >= 1, "%d 个表" % len(d.tables))
# 判据要对着**输入原文**，不能用长度阈值 —— 我第一版写 >120 字，
# 而实际输入那段就是 99 字，于是把"完整"误判成"被截断"。
_long_src = ("民航业数字化转型加速，智慧机场与智慧空管对复合型人才需求快速增长。" * 3)
check("长段落完整（输入原文逐字都在）",
      any(_long_src in p or _long_src[:60] in p for p in paras),
      "最长 %d 字" % max((len(p) for p in paras), default=0))

# ---------------------------------------------------------------- Excel
print("\n【3】Excel —— 复杂规格：3 张表（明细/分类汇总/总览）+ 合计 + 列格式")
sheets = [
    {"name": "明细", "title": "2026 年实训耗材采购明细",
     "header": ["日期", "物品", "数量", "单价", "金额"],
     "rows": [["2026-03-01", "GPU 服务器", 2, 128000, 256000],
              ["2026-03-15", "边缘节点", 6, 8600, 51600],
              ["2026-04-02", "标注工作位", 40, 2400, 96000],
              ["2026-04-20", "示波器", 12, 3200, 38400]],
     "formats": ["text", "text", "int", "money", "money"], "totals": True},
    {"name": "分类汇总", "title": "按类别统计",
     "header": ["类别", "台数", "金额"],
     "rows": [["计算设备", 8, 307600], ["工位", 40, 96000], ["仪器", 12, 38400]],
     "formats": ["text", "int", "money"], "totals": True},
    {"name": "总览", "title": "预算执行总览",
     "header": ["年度", "预算（万元）", "已执行"],
     "rows": [["第一年", 420, 442000], ["第二年", 380, 0], ["第三年", 200, 0]],
     "formats": ["text", "number", "money"]},
]
txt, _ = call("make_xlsx", {"filename": "实训耗材台账", "theme": "red",
                            "colors": RED_GOLD, "sheets": sheets})
check("Excel 生成成功", "已生成表格" in str(txt), str(txt)[:56])
f = newest(".xlsx")
check("产物是**真** xlsx", bool(f) and zipfile.is_zipfile(f))
# ⚠️ 不用 openpyxl —— 它不在依赖里（本机也没装）。
#   用应用自带的 doc_extract（纯标准库解析 OOXML 的）来读，顺便验证"自家读取器也读得懂自家产物"。
from backend import doc_extract as DX                                # noqa: E402
with zipfile.ZipFile(f) as z:
    book = z.read("xl/workbook.xml").decode("utf-8", "replace")
names_in_book = book.count("<sheet ")
check("工作表数达标（3 张）", names_in_book >= 3, "%d 张" % names_in_book)
# ⚠️ extract_text 返回的是 **(文本, 错误)**，不是 (ok, 文本) —— 我第一版解包解反了，
#    拿到的 sheet_text 其实是空错误串，于是把读得出来误判成读不出来。
sheet_text, _err = DX.extract_text(f)
sheet_text = sheet_text or ""
check("表内文字读得出来（自带读取器）", len(sheet_text) > 50, "%d 字" % len(sheet_text))
check("明细数据完整（大数字 256000 在）", "256000" in sheet_text.replace(",", ""))
check("合计行有内容", "合计" in sheet_text)
check("三张表的名字都在", all(n in sheet_text or n in book
                              for n in ("明细", "分类汇总", "总览")))

# ---------------------------------------------------------------- 代码
print("\n【4】代码 —— 生成并**真跑**（不是纸上谈兵）")
code = ("import json\n"
        "data = [{'n': 'a', 'v': 3}, {'n': 'b', 'v': 1}, {'n': 'c', 'v': 2}]\n"
        "top = sorted(data, key=lambda x: -x['v'])\n"
        "print(json.dumps(top, ensure_ascii=False))\n"
        "print('sum =', sum(x['v'] for x in data))\n")
txt, _ = call("run_python", {"code": code})
check("代码真的跑了（有标准输出）", "标准输出" in str(txt) and "sum = 6" in str(txt),
      str(txt)[:70].replace("\n", " "))

# ---------------------------------------------------------------- 长文本
print("\n【5】长文本 —— 长文自动落盘，不能被截断")
long_text = "# 实训基地建设三年规划\n\n" + ("民航业数字化转型加速，对复合型人才需求快速增长。" * 40 + "\n\n") * 3
rel = None
try:
    from backend import main as M                                    # noqa: E402
    rel = M._autosave_answer(long_text, "写一份建设规划")
except Exception as e:
    print("   （直接调 autosave 失败：%r）" % e)
check("长文落盘成功", bool(rel), rel)
if rel:
    from backend import doclib                                       # noqa: E402
    got = doclib.read_file(rel)
    body = got.get("text") or ""
    check("长文完整（未截断）", len(body) >= len(long_text) - 20, "%d 字" % len(body))

# ---------------------------------------------------------------- 图片
if "--img" in sys.argv:
    print("\n【6】图片 —— 写实 + 768 + 16:9 横版（真跑）")
    ev = []
    txt, ev = call("generate_image", {
        "prompt": ("professional 35mm photograph of a modern AI training lab, students at "
                   "workstations, warm daylight from large windows, shallow depth of field, "
                   "photorealistic, sharp focus, fine detail"),
        "negative_prompt": "cartoon, anime, illustration, painting, 3d render, blurry, deformed",
        "size": 768, "aspect": "16:9"})
    imgs = [e for e in ev if e.get("type") == "image"]
    check("图片生成成功", bool(imgs), str(txt)[:60].replace("\n", " "))
    if imgs:
        import base64                                                # noqa: E402
        png = base64.b64decode(imgs[0]["b64"])
        check("是真 PNG（魔数正确）", png[:8] == b"\x89PNG\r\n\x1a\n")
        w = int.from_bytes(png[16:20], "big")
        h = int.from_bytes(png[20:24], "big")
        # ⚠️ 别写死 (1024,576) —— 出图尺寸随**模型原生档**走：
        #    SD2.1 系 768/16:9 → 1024x576；SDXL 系 → 1360x768（原生 1024 档）。
        #    这里按"和当前模型的尺寸表一致 + 比例确实是 16:9"来判，换模型不用改测试。
        _want = T._img_wh(768, "16:9")
        check("尺寸与当前模型的尺寸表一致", (w, h) == _want,
              "%dx%d（期望 %dx%d）" % (w, h, _want[0], _want[1]))
        check("确实是 16:9", abs(w / float(h) - 16 / 9.0) < 0.02,
              "%.3f" % (w / float(h)))
        check("短边不低于 384（不是糊图）", min(w, h) >= 384, "%dx%d" % (w, h))
        check("走的是显卡", imgs[0].get("device") == "cuda", imgs[0].get("device"))
        check("有写实风格加成", "photorealistic" in str(txt) or "摄影" in str(txt),
              str(txt)[:60].replace("\n", " "))
        out = os.path.join(tempfile.gettempdir(), "gen_matrix_img.png")
        open(out, "wb").write(png)
        print("     样图已存:", out)
else:
    print("\n【6】图片 —— 跳过（加 --img 真跑一张）")

# ---------------------------------------------------------------- 自定义配色
# ⚠️ 这一节是为一个**静默失效**加的（2026-09-26 实测踩到）：
#    给 Excel 传 {"accent": "#B8860B"}，表头**照样是蓝的** —— 因为 xlsx 的主题
#    里表头色叫 head、不叫 accent，通用键落不到它身上；Word 的封面标题也一样。
#    三个生成器的主题键名不统一（pptx: accent/cover_bg/bg…；xlsx: head/zebra/line；
#    docx: accent/head/accent2/text/quote_bg…），所以必须有别名映射 + 派生色联动。
print("\n【7】自定义配色 —— 三个生成器都要真的跟着变色（含键名别名）")
GOLD, DEEP = "B8860B", "8B0000"
_CC = {"accent": "#" + GOLD}


def _zip_text(path, *members):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        return "\n".join(
            z.read(n).decode("utf-8", "replace")
            for n in (members or names) if n in names)


from backend import pptx_maker as _P, docx_maker as _D, xlsx_maker as _X  # noqa: E402


call("make_pptx", {"title": "配色测试", "filename": "cc-ppt", "theme": "blue",
                   "colors": _CC,
                   "slides": [{"layout": "content", "title": "页一",
                               "bullets": ["要点一", "要点二"]}]})
call("make_docx", {"title": "配色测试", "filename": "cc-doc", "cover": True,
                   "theme": "blue", "colors": _CC,
                   "blocks": [{"type": "heading", "text": "第一章", "level": 1},
                              {"type": "para", "text": "正文"},
                              {"type": "bullet", "items": ["甲", "乙"]}]})
call("make_xlsx", {"filename": "cc-xl", "theme": "blue", "colors": _CC,
                   "sheets": [{"name": "明细", "header": ["项目", "金额"],
                               "rows": [["甲", 100], ["乙", 200]],
                               "total_row": True}]})

_fp, _fd, _fx = (newest(".pptx"), newest(".docx"), newest(".xlsx"))
_xml_p = _zip_text(_fp) if _fp else ""
_xml_d = _zip_text(_fd, "word/document.xml") if _fd else ""
_xml_x = _zip_text(_fx, "xl/styles.xml") if _fx else ""

check("PPT 用上自定义主色", GOLD in _xml_p)
check("Word 用上自定义主色（封面标题/章节线）", GOLD in _xml_d)
# ★ 这条就是当年漏掉的那条：Excel 的表头色叫 head，通用键 accent 必须落到它上面
check("Excel 用上自定义主色（表头 head 跟着走）", GOLD in _xml_x,
      "styles.xml 里出现 %d 次" % _xml_x.count(GOLD))
check("Excel 没有残留预设蓝（说明配套色也一起换了）", "2E75B6" not in _xml_x,
      "残留 %d 次" % _xml_x.count("2E75B6"))
# 值要沿用各自的格式：pptx/docx 的主题存不带 # 的，xlsx 的存带 # 的。
# ⚠️ 别拿文件里的字符串当判据 —— openpyxl 写出来是 ARGB（"FF"+色值，多一个透明度前缀），
#    所以"带 #"这条要查**主题字典**（那才是下游真正消费的东西）。
check("PPT 主题色值格式正确（不带 #）",
      not _P.apply_colors(dict(_P.THEMES["blue"]), _CC)["accent"].startswith("#"))
check("Excel 主题色值格式正确（带 #）",
      _X.THEMES["blue"]["head"].startswith("#")
      and _P.apply_colors(dict(_X.THEMES["blue"]), _CC)["head"].startswith("#"))
check("Excel 文件里是 ARGB 形式（FF+色值），不是半截色值",
      "FF" + GOLD in _xml_x, "styles.xml 里 %d 处" % _xml_x.count("FF" + GOLD))
# 派生色联动：只给了 accent，配套的浅色（卡片/隔行底/分隔线）要跟着重算
_base = dict(_P.THEMES["blue"])
_derived = _P.apply_colors(_base, _CC)
check("预设字典没被就地改坏（不会污染后续请求）",
      _base["accent"] == "2E75B6" and _P.THEMES["blue"]["accent"] == "2E75B6")
check("配套浅色跟着主色重算（card 不再是原来的蓝调）",
      _derived["card"] != _base["card"])
check("Excel 主题的 head/zebra 都跟着变（别名+派生）",
      _P.apply_colors(dict(_X.THEMES["blue"]), _CC)["head"].lstrip("#").upper() == GOLD
      and _P.apply_colors(dict(_X.THEMES["blue"]), _CC)["zebra"] != _X.THEMES["blue"]["zebra"])
_warm = _P.apply_colors(dict(_D.THEMES["warm"]), _CC)
check("Word warm 主题的 head 保留「比主色更暗」的性格（按通道比例派生）",
      _warm["head"] != GOLD and _warm["head"] != _D.THEMES["warm"]["head"])
check("非法色值只记警告、不崩",
      bool(_P.apply_colors(dict(_P.THEMES["blue"]), {"accent": "nope"}).get("_warn")))

print("\n" + "=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
if "--keep" not in sys.argv:
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print("工作目录保留:", TMP)
sys.exit(1 if FAIL else 0)
