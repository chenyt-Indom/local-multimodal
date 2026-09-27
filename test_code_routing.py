# -*- coding: utf-8 -*-
"""守住"要不要切到专用代码模型"的判定 —— 误判的代价是**整轮多等 10 秒以上**。

背景（2026-09-22 实测）：
  用户抱怨"发出去要等很久才开始输出"。查下来有一类原因是**模型被误切**：
  演示会话里
    「你好，我叫陈工，在做本地 AI 应用」     → 被判成写代码 ❌
    「帮我用一句话解释什么是向量数据库」     → 被判成写代码 ❌
    「再帮我说说它和普通数据库的区别」       → 被判成写代码 ❌
  于是每一轮都在 qwen3-vl:8b（默认）和 qwen2.5-coder:14b 之间来回切；
  换模型 = 卸载 + 重新加载数 GB → 那一轮的首字延迟 15~17 秒，
  而**同一个模型连着的下一轮只要 0.7 秒**。

  误判的两个来源（已修）：
    ① `_CODE_ACTIONS` 里有"帮我 / 给我 / 生成 / 完成" —— 它们是**通用请求口气**，
       配上一个弱术语（数据库 / 函数 / 接口…）必然命中；
    ② `_BUILD_VERBS` 全是**光杆动词**（做/写/搞/弄…）—— "在做 xx 应用"这种
       自我介绍也会命中"做 + 应用"。
  修法：动作词只留真动作；造物口吻改成"帮我 / 做个 / 写个 / 我想要"这类**搭配**。

跑法：python test_code_routing.py   （必须用 Python 3.14）
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_route_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import main as M       # noqa: E402
from backend import config as C     # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


# ---------------- 不该判成代码任务的（实测踩过的原话） ----------------
NOT_CODE = [
    ("自我介绍里提到在做应用", "你好，我叫陈工，在做本地 AI 应用"),
    ("让我解释一个技术名词", "帮我用一句话解释什么是向量数据库"),
    ("接着问同一个话题", "再帮我说说它和普通数据库的区别"),
    ("介绍自己会什么", "我平时主要用 Python，也会一点前端"),
    ("让我给建议", "最后给我三个选型时的建议，各一句话"),
    ("问概念", "什么是递归？帮我用大白话说说"),
    ("问有哪些产品", "数据库有哪些主流产品？各自适合什么场景"),
    ("润色文字", "帮我看看这段话有没有语病"),
    ("纯寒暄", "你好"),
    ("问时间", "现在几点了"),
]
# ---------------- 该判成代码任务的 ----------------
IS_CODE = [
    ("写函数", "写个排序函数"),
    ("指定语言 + 写", "帮我用 Python 写一个合并两个有序列表的函数"),
    ("大白话造物", "帮我做一个小网页，番茄钟"),
    ("我想要个东西", "我想要个记账的小程序"),
    ("改代码", "帮我改一下这段代码"),
    ("贴了代码块", "```python\nprint(1)\n```"),
    ("强特征：调试", "帮我调试一下这个报错"),
    ("脚本 + 动作", "帮我跑一下这个脚本"),
    ("强特征：爬虫", "来个爬虫抓一下这个网页的标题"),
    ("强特征：帮我实现", "帮我实现一个登录接口"),
    ("SQL + 动作", "优化一下这段 SQL"),
    ("造物口吻 + 产物", "做个待办清单的网页"),
]

print("=" * 62)
print("① 不该切到代码模型的（误判 = 每轮多等十几秒）")
for label, text in NOT_CODE:
    got = M._is_code_task(text)
    check("%s：%s" % (label, text[:24]), not got, f"被判成代码任务（实得 {got}）")

print()
print("=" * 62)
print("② 该切到代码模型的（漏判 = 思考型模型写代码很慢）")
for label, text in IS_CODE:
    got = M._is_code_task(text)
    check("%s：%s" % (label, text.replace("\n", " ")[:24]), got, "没被判成代码任务")

print()
print("=" * 62)
print("③ 走完整路由函数（含开关、模型是否已下载）")
cfg = dict(C.load_config())
cfg["code_auto_route"] = True
cfg["code_model"] = "qwen2.5-coder:14b"
M._installed_models = lambda: {"qwen3-vl:8b", "qwen2.5-coder:14b"}   # 假装已下载

m1, n1 = M._route_code_model(cfg, "帮我用一句话解释什么是向量数据库", "qwen3-vl:8b")
check("解释类问题：留在默认模型", m1 == "qwen3-vl:8b" and not n1, f"{m1} / {n1}")
m2, n2 = M._route_code_model(cfg, "帮我做一个小网页，番茄钟", "qwen3-vl:8b")
check("大白话造物：切到代码模型并给出提示",
      m2 == "qwen2.5-coder:14b" and "专用模型" in n2, f"{m2} / {n2}")
m3, _ = M._route_code_model(cfg, "你好，我叫陈工，在做本地 AI 应用", "qwen3-vl:8b")
check("自我介绍：不切模型", m3 == "qwen3-vl:8b", m3)

cfg_off = dict(cfg)
cfg_off["code_auto_route"] = False
m4, _ = M._route_code_model(cfg_off, "帮我写个排序函数", "qwen3-vl:8b")
check("关掉「专用模型」开关后一律不切", m4 == "qwen3-vl:8b", m4)

print()
print("=" * 62)
print("④ 动作词表里不许再出现通用请求口气")
for w in ("帮我", "给我", "生成", "完成"):
    check("动作词表已移除「%s」" % w, w not in M._CODE_ACTIONS)
check("光杆动词表已废弃（_BUILD_VERBS 不再被使用）",
      not hasattr(M, "_BUILD_VERBS"))
check("造物口吻表里有「做个 / 写个 / 我想要」等搭配",
      "做个" in M._BUILD_REQ and "写个" in M._BUILD_REQ and "我想要" in M._BUILD_REQ)
check("⚠️ 「做个」不能退化成光杆「做」", "做" not in M._BUILD_REQ,
      "「做」单独在表里，会再次误伤「在做 xx 应用」")
check("弱术语 + 光杆动作仍能命中（API 没被削弱）",
      M._is_code_task("帮我改一下这段 python 代码"))

print()
print("=" * 62)
print("⑤ 多轮：文档任务（作文 / PPT / Excel）不许被误切到代码模型")
# 2026-09-27 用户报："有时候让它写作文、做 PPT 或 Excel，结果给我用了代码模型。"
# 现场：上一轮做 Excel 时助手回答里贴了一段 python 代码（"已生成，代码如下：…"），
# 这一轮只说「再帮我加一列合计」——一个写作/办公关键词都没有，只有接续词"再/加"，
# 于是被判成"还在改代码" → 切 qwen3-coder。伤害不只是模型换错：**代码模型那一轮
# 工具被整轮清空**（见 code_model_on），make_xlsx / make_pptx / edit_office 全没了，
# 用户不是"感觉不对"，是**根本改不动那份文件**。离线实测 12 条全中。
REPLY_WITH_CODE = ("已生成《月度开支.xlsx》，代码如下：\n"
                   "```python\nfrom openpyxl import Workbook\n```\n文件已保存到生成文库。")
DOC_ITERATION = [
    ("帮我做一个 Excel 表格，记录这个月开支", "再帮我加一列合计", "Excel 加列"),
    ("帮我做一个 Excel 表格", "再加一行小计", "Excel 加行"),
    ("帮我做一个 Excel 表格", "顺便把表格底色换成浅灰", "Excel 改样式"),
    ("帮我做一个 Excel 表格", "再帮我加个图表", "Excel 加图表"),
    ("帮我做一个 Excel 表格", "再帮我做个 Excel 汇总表", "Excel 再造一份"),
    ("帮我做一个介绍广州的 PPT", "再加一页讲公司简介", "PPT 加页"),
    ("帮我做一个介绍广州的 PPT", "把第 3 页改一下", "PPT 改页"),
    ("帮我做一个介绍广州的 PPT", "再改一下封面标题", "PPT 改标题"),
    ("帮我写一篇关于春天的作文", "再改一下开头", "作文改开头"),
    ("帮我写一篇关于春天的作文", "再帮我加一段", "作文加段"),
    ("帮我写一篇关于春天的作文", "继续，再加个结尾", "作文加结尾"),
    ("帮我写一份工作总结", "再补充一下第二点", "总结补充"),
]
for pu, cur, why in DOC_ITERATION:
    _msgs = [{"role": "user", "content": pu},
             {"role": "assistant", "content": REPLY_WITH_CODE},
             {"role": "user", "content": cur}]
    got = M._prev_code_flag(_msgs, cur)
    check("%s：%s → 不判成代码迭代" % (why, cur[:18]), not got,
          "被当成还在改代码 → 会切代码模型且工具被清空")
    m, _ = M._route_code_model(cfg, cur, "qwen3-vl:8b", prev_code=got)
    check("%s：留在默认模型" % why, m == "qwen3-vl:8b", m)

print()
print("=" * 62)
print("⑥ 多轮：真·代码迭代仍必须切（别把这个修坏）")
CODE_ITERATION = [
    ("帮我写一个网页，番茄钟", "再帮我加个深色模式"),
    ("帮我写一个网页，番茄钟", "再改一处样式"),
    ("帮我写一个网页，番茄钟", "继续优化性能"),
    ("帮我用 python 写个脚本读 csv", "再改成支持 xlsx"),   # 句中有 xlsx，但这是代码迭代
    ("帮我写个爬虫抓标题", "再加个存数据库的功能"),
]
for pu, cur in CODE_ITERATION:
    _msgs = [{"role": "user", "content": pu},
             {"role": "assistant", "content": REPLY_WITH_CODE},
             {"role": "user", "content": cur}]
    got = M._prev_code_flag(_msgs, cur)
    check("代码迭代：%s → %s 仍判代码" % (pu[:12], cur[:14]), got, "漏判，思考型模型写代码很慢")
    m, _ = M._route_code_model(cfg, cur, "qwen3-vl:8b", prev_code=got)
    check("代码迭代：切到代码模型", m == "qwen2.5-coder:14b", m)

print()
print("=" * 62)
print("⑦ 多轮：跨任务边界（改主意时要跟得上）")
_msgs = [{"role": "user", "content": "帮我做一个介绍广州的 PPT"},
         {"role": "assistant", "content": REPLY_WITH_CODE},
         {"role": "user", "content": "再帮我写个 python 脚本读这个 pptx"}]
_cur = "再帮我写个 python 脚本读这个 pptx"
check("文档 → 代码：用户改主意要代码，仍切代码模型",
      M._route_code_model(cfg, _cur, "qwen3-vl:8b",
                          prev_code=M._prev_code_flag(_msgs, _cur))[0] == "qwen2.5-coder:14b")
_msgs = [{"role": "user", "content": "帮我写个读 csv 的脚本"},
         {"role": "assistant", "content": REPLY_WITH_CODE},
         {"role": "user", "content": "再帮我做个 Excel 汇总表"}]
_cur = "再帮我做个 Excel 汇总表"
check("代码 → 要文件：留在默认模型（保住 make_xlsx，否则拿不到文件）",
      M._route_code_model(cfg, _cur, "qwen3-vl:8b",
                          prev_code=M._prev_code_flag(_msgs, _cur))[0] == "qwen3-vl:8b")
check("上一轮只是闲聊天、这轮说「再加一句」：不判代码迭代",
      not M._prev_code_flag([{"role": "user", "content": "你好"},
                             {"role": "assistant", "content": "你好，有什么可以帮你？"},
                             {"role": "user", "content": "再加一句"}], "再加一句"))

print()
print("=" * 62)
print("⑧ 带图片的轮次：绝不允许切到代码模型（代码模型没有视觉能力）")
# 2026-09-27：`ollama show qwen3-coder:30b` 的能力段里**只有 completion / tools，
# 没有 vision**，而路由原来只看文本、不看有没有图 —— 于是这些说法都会把图丢掉：
#   · 「这张截图里的报错怎么修，帮我改一下」 → "报错 + 改" 命中 → 切 ❌
#   · 「分析一下这张图的数据，写个脚本处理」 → "脚本 + 写" 命中 → 切 ❌
#   · 上一轮写过代码、这轮贴图说「再帮我改两处」 → prev_code 命中 → 切 ❌
# 用户主动贴图就是最强的"我要你看图"信号，看不到图 = 白传。
IMAGE_TURNS = [
    ("这张截图里的报错怎么修，帮我改一下", False),
    ("分析一下这张图的数据，写个脚本处理", False),
    ("帮我把这份 PPT 内容改成代码示例", False),
    ("再帮我改两处", True),
    ("帮我看看这张图的代码哪里错了", False),
    ("把这张图里的表格转成 Excel", False),
]
for text, pc in IMAGE_TURNS:
    m, _ = M._route_code_model(cfg, text, "qwen3-vl:8b", prev_code=pc, has_images=True)
    check("带图：%s → 留在视觉模型" % text[:18], m == "qwen3-vl:8b", m)

check("⚠️ 不带图时同一句话仍会切（不能把正常路由修坏）",
      M._route_code_model(cfg, "分析一下这张图的数据，写个脚本处理", "qwen3-vl:8b")[0]
      == "qwen2.5-coder:14b")
check("带图 + 上一轮写过代码，也不切",
      M._route_code_model(cfg, "再帮我改两处", "qwen3-vl:8b",
                          prev_code=True, has_images=True)[0] == "qwen3-vl:8b")
_m, _n = M._route_code_model(cfg, "帮我写个脚本", "qwen3-vl:8b",
                             force=True, has_images=True)
check("开发台带图：也留在视觉模型，并说明原因", _m == "qwen3-vl:8b" and "看不了图" in _n,
      "%s / %s" % (_m, _n))
check("开发台不带图：仍进代码模型",
      M._route_code_model(cfg, "帮我写个脚本", "qwen3-vl:8b", force=True)[0]
      == "qwen2.5-coder:14b")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
