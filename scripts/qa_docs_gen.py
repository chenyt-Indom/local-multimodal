# -*- coding: utf-8 -*-
"""验证 Word / PPT / Excel 的生成质量。

不测代码，测**产物**：生成后打开文件，逐项检查
结构完整性、样式是否生效、中文是否正常、公式/合计是否正确。
"""
import os
import sys
import time

sys.path.insert(0, r"D:\local-multimodal-src")
os.environ["MM_DATA_DIR"] = r"D:\local-multimodal-src"
sys.stdout.reconfigure(encoding="utf-8")

from backend import tools as T      # noqa: E402

OUT = r"D:\local-multimodal-src\_qa_docs"
os.makedirs(OUT, exist_ok=True)


def _paths(ev):
    return [p for p in (ev or []) if isinstance(p, str)]


results = []


# ---------------- 1) Word 文档 ----------------
print("=" * 60)
print("【1】Word 文档生成")
print("=" * 60)
ev = []
t0 = time.time()
r = T.dispatch("make_docx", {
    "title": "中小企业数字化转型调研报告",
    "subtitle": "基于 2026 年三季度走访数据的分析",
    "author": "信息化推进小组",
    "date_text": "2026 年 9 月",
    "theme": "business",
    "toc": True,
    "cover": True,
    "blocks": [
        {"type": "heading", "text": "一、调研背景", "level": 1},
        {"type": "paragraph", "text":
            "为摸清中小企业在数字化转型中的真实处境，本次调研走访了 3 个行业共 42 家企业，"
            "覆盖制造、零售与专业服务。调研方式包括实地访谈、问卷与系统日志抽样。"},
        {"type": "heading", "text": "二、主要发现", "level": 1},
        {"type": "bullet", "items": [
            "**管理层意愿强、执行层动力弱**：78% 的企业负责人把数字化列为年度重点，但仅 31% 有专职推进人员。",
            "**数据资产散落**：63% 的企业关键经营数据分散在至少 3 个互不连通的系统中。",
            "**投入集中在可见环节**：预算多投向 OA、考勤等易见效场景，生产与供应链改造占比不足 20%。",
        ]},
        {"type": "heading", "text": "三、原因分析", "level": 1},
        {"type": "paragraph", "text":
            "短期业绩压力与转型回报周期之间存在结构性错配。企业更愿意为"
            "当期可见的效率改善付费，而数据打通、流程重构的价值往往在一到两年后才显现。"},
        {"type": "heading", "text": "四、对策建议", "level": 1},
        {"type": "number", "items": [
            "先打通一条**端到端**的业务链，而不是全面铺开。",
            "把数据治理纳入部门考核，避免只做系统上线不做数据维护。",
            "建立可分阶段验收的投入机制，降低一次性投入的决策门槛。",
        ]},
        {"type": "heading", "text": "五、结论", "level": 1},
        {"type": "paragraph", "text":
            "中小企业的数字化转型瓶颈，主要不在技术选型，而在组织能力与投入节奏的匹配。"},
    ],
}, lambda e: ev.append(e), {})
dt = time.time() - t0
paths = _paths(ev)
print("  返回:", str(r)[:150].replace("\n", " "))
print("  生成 %d 个文件，%.1fs" % (len(paths), dt))
results.append(("make_docx", r, paths))


# ---------------- 2) PPT ----------------
print()
print("=" * 60)
print("【2】PPT 生成")
print("=" * 60)
ev = []
t0 = time.time()
r = T.dispatch("make_pptx", {
    "title": "2026 年第三季度项目汇报",
    "subtitle": "研发中心 · 阶段性进展与下季计划",
    "author": "研发中心",
    "theme": "tech",
    "slides": [
        {"layout": "cover", "title": "2026 年第三季度项目汇报",
         "subtitle": "研发中心 · 阶段性进展与下季计划"},
        {"title": "本季概览", "bullets": [
            "交付 **4 个** 迭代版本，准时率 ==100%==。",
            "线上故障数环比下降 38%。",
            "核心接口 P95 延迟从 420ms 降到 210ms。",
            "新入职 6 人，已完成上岗培训。",
        ]},
        {"title": "关键成果", "bullets": [
            "**架构升级**：完成从单体到模块化的拆分，发布周期由两周缩短到三天。",
            "**性能优化**：引入缓存与批量写入，数据库压力下降约六成。",
            "**质量建设**：补齐端到端测试，回归成本下降明显。",
        ]},
        {"title": "问题与风险", "bullets": [
            "历史模块文档缺失，新人上手成本高。",
            "测试环境与生产环境存在配置差异。",
            "跨部门接口联调周期偏长。",
        ]},
        {"layout": "two_col", "title": "下季重点", "left_title": "技术侧", "right_title": "协作侧",
         "left": ["完善可观测性建设", "推进灰度发布", "补齐模块文档"],
         "right": ["建立接口对接例会", "统一环境配置基线", "开展内部技术分享"]},
        {"title": "关键指标展望", "bullets": [
            "发布周期：3 天 → 1 天",
            "P95 延迟：210ms → 150ms",
            "线上故障：再降 30%",
        ]},
        {"layout": "end", "title": "感谢聆听", "subtitle": "欢迎提问与指正"},
    ],
}, lambda e: ev.append(e), {})
dt = time.time() - t0
paths = _paths(ev)
print("  返回:", str(r)[:150].replace("\n", " "))
print("  生成 %d 个文件，%.1fs" % (len(paths), dt))
results.append(("make_pptx", r, paths))


# ---------------- 3) Excel ----------------
print()
print("=" * 60)
print("【3】Excel 生成")
print("=" * 60)
ev = []
t0 = time.time()
r = T.dispatch("make_xlsx", {
    "theme": "business",
    "sheets": [{
        "name": "费用明细",
        "title": "研发中心 2026 年三季度费用明细",
        "header": ["部门", "科目", "预算（元）", "实际支出（元）", "执行率", "备注"],
        "formats": ["text", "text", "money", "money", "percent", "text"],
        "rows": [
            ["研发一部", "人力成本", 480000, 462000, 0.9625, "含社保公积金"],
            ["研发一部", "设备采购", 120000, 118500, 0.9875, "服务器扩容"],
            ["研发二部", "人力成本", 520000, 531000, 1.0212, "含加班费"],
            ["研发二部", "云服务", 90000, 76400, 0.8489, "按量计费"],
            ["测试部", "人力成本", 260000, 248000, 0.9538, ""],
            ["测试部", "测试设备", 45000, 41200, 0.9156, ""],
            ["综合管理", "办公费用", 30000, 27600, 0.9200, ""],
            ["综合管理", "培训费用", 60000, 58000, 0.9667, "外部讲师"],
        ],
        "total_row": True,
    }],
}, lambda e: ev.append(e), {})
dt = time.time() - t0
paths = _paths(ev)
print("  返回:", str(r)[:150].replace("\n", " "))
print("  生成 %d 个文件，%.1fs" % (len(paths), dt))
results.append(("make_xlsx", r, paths))

print()
print("=" * 60)
print("产出文件：")
for name, r, paths in results:
    print("  %-12s %s" % (name, paths or "（无文件）"))
