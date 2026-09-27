# -*- coding: utf-8 -*-
"""守住"该联网核实就联网"的两件事 —— ① 工具得在手上 ② 该搜时得知道要搜。

背景（2026-09-27 用户实测反馈）：
  「让模型写产品介绍文时，模型没有联网了解产品就写作。」
  查下来是**两个独立缺陷叠加**，缺一个都修不好：

  ① **工具根本不在手上**（必要条件）
     `tools.make_schemas` 在「长文创作 writing=True」和「办公产物 office=True」
     两个分支里把 web_search / web_read 一起砍掉了 —— 写作轮只给
     [ask_user, library, search_knowledge]。
     所以模型不是"不肯搜"，是**看不到这个工具、没法搜**。
     实测代价很小：web_search 303 + web_read 152 = 455 token（num_ctx 40960 完全放得下）。

  ② **该搜的判据太窄**（充分条件）
     系统提示里原来写的是"日常闲聊、**写作**、翻译、代码等不需要联网的任务不要调用"
     —— 等于明说"写作别搜"。`_wants_search` 也只认"搜索/查一下"这类词，
     「写一份 XX 产品的介绍文」一个词都不命中，于是既不提醒、也不搜。

跑法：python test_web_intent.py   （必须用 Python 3.14）
"""
import io
import os
import shutil
import sys
import tempfile

# ⚠️ 必须带 line_buffering：不带的话重定向到日志时整块缓存，跑一半看不到进度
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_webint_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import main as M       # noqa: E402
from backend import tools as T      # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


# ---------------- ① 工具必须到手：写作 / 办公模式下要给联网工具 ----------------
def names(**kw):
    return {s["function"]["name"]
            for s in T.make_schemas(True, True, True, ask_mode="deep", **kw)}


print("== 联网工具必须在写作 / 办公模式里可用 ==")
for label, kw in (("长文创作 writing", dict(writing=True)),
                  ("办公产物 office", dict(office=True)),
                  ("办公重试 office_gen", dict(office=True, office_gen=True, writing=False))):
    ns = names(**kw)
    check("%s：有 web_search" % label, "web_search" in ns, sorted(ns))
    check("%s：有 web_read" % label, "web_read" in ns, sorted(ns))

# ---------------- ② 开关边界：关掉联网就不许给 ----------------
print()
print("== 关掉「联网」开关时，这些工具必须消失（守住\"关着不联网\"的约定） ==")
for label, kw in (("长文创作 writing", dict(writing=True)), ("办公产物 office", dict(office=True))):
    ns = {s["function"]["name"]
          for s in T.make_schemas(False, True, True, ask_mode="deep", **kw)}
    check("%s：关着联网时没有 web_search" % label, "web_search" not in ns, sorted(ns))
    check("%s：关着联网时没有 web_read" % label, "web_read" not in ns, sorted(ns))

# 办公模式还必须留住生成工具（别为了加联网把生成工具挤掉）
print()
print("== 办公模式仍必须留生成工具（老问题的回归） ==")
_off = names(office=True)
for t in ("make_pptx", "make_docx", "make_xlsx", "edit_office", "library", "ask_user"):
    check("办公模式保留 " + t, t in _off, sorted(_off))

# ---------------- ③ 该搜的判据 ----------------
print()
print("== 该联网核实的（用户报的场景 + 同类） ==")
SHOULD = [
    "帮我写一份扫地机器人的产品介绍文",
    "写一篇 XX 手机的产品介绍",
    "帮我写一份公司新项目的介绍",
    "写一份新能源汽车行业的分析报告",
    "整理一下 2026 年数据安全相关的政策法规",
    "帮我写一篇关于量子纠缠的科普文章",
    "介绍一下深度学习的基本原理",
    "帮我写一份竞品对比分析",
    "做个 PPT 介绍我们公司的产品线",
    "帮我查一下这个软件的用法",
    "搜一下最近有什么新政策",
]
for text in SHOULD:
    check("该搜：%s" % text, M._wants_search(text))

print()
print("== 不该联网核实的（闲聊 / 纯创作 / 个人事务 / 纯代码） ==")
SHOULD_NOT = [
    "你好，我叫陈工",
    "介绍一下你自己",
    "写一篇关于春天的散文",
    "帮我写一首诗，主题是秋天",
    "写一封家书给爸爸",
    "写一篇我的寒假生活作文",
    "帮我写一份个人工作计划",
    "写一段自我介绍",
    "写一份求职简历",
    "帮我把这段话翻译成英文",
    "帮我写个 python 脚本读 csv",
    "写一个快速排序函数",
    "今天心情不好，陪我聊聊",
    "帮我做个 Excel 记账表格",
    "写一段祝福语给同事",
    "帮我把上面那段改得通顺一点",
]
for text in SHOULD_NOT:
    check("不该搜：%s" % text, not M._wants_search(text))

# ---------------- ④ 提醒的触发面：长文/办公任务默认要核实 ----------------
print()
print("== 长文/办公任务：不是纯创作就该提醒（型号类产品靠这条兜住） ==")
# (句子, 是否长文/办公任务, 期望是否提醒)
LONG_FORM = [
    # —— 用户报的场景与同类 ——
    ("帮我写一份扫地机器人的产品介绍文", True, True),
    ("帮我做个 PPT，介绍「大疆 Mini 4 Pro 无人机」，6 页左右。", True, True),
    ("帮我做个 PPT，介绍我们公司的新产品「智能指纹门锁 K9」", True, True),
    ("帮我写一份竞品对比分析", True, True),
    # ⚠️ 个人工作计划**也该提醒**：文案里的例外条款会让模型改去 ask_user 要材料，
    #    而不是去网上瞎搜 —— 这比"什么都不说、直接编"好。
    ("帮我写一份个人工作计划", True, True),
    # —— 纯创作：不该提醒（硬拉去搜会把无关材料塞进文章）——
    ("写一篇关于秋天的散文，800 字左右", True, False),
    ("帮我写一首诗，主题是秋天", True, False),
    ("写一封家书给爸爸", True, False),
    ("写一篇我的寒假生活作文", True, False),
    ("写一段祝福语给同事", True, False),
    # —— 非长文任务：只按 _wants_search 走 ——
    ("你好，我叫陈工", False, False),
    ("介绍一下你自己", False, False),
    ("帮我把这段话翻译成英文", False, False),
    ("帮我写个 python 脚本读 csv", False, False),
    ("帮我查一下 iPhone 17 的价格", False, True),
    ("介绍一下大疆 Mini 4 Pro 无人机", False, True),
]
for text, long_form, want in LONG_FORM:
    check("%s：%s" % ("该提醒" if want else "不该提醒", text),
          M._needs_web_ctx(text, long_form) == want,
          "long_form=%s 实际=%s" % (long_form, M._needs_web_ctx(text, long_form)))

# ---------------- ⑤ 提醒必须并进第一条 system（不能另起一条） ----------------
print()
print("== 联网提醒的注入方式 ==")
src = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
check("提醒文案存在于 main.py", "动笔前的准备" in src)
check("提醒里点明「不改变任务本身」", "只是一个前置步骤，不改变本轮任务" in src)
check("没有再用 messages.append 追加第二条 system 的旧写法",
      '"role": "system", "content": (\n            "用户这句话看起来需要最新信息' not in src)

print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
if FAIL:
    print("失败清单：")
    for f in FAIL:
        print("  - " + f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
