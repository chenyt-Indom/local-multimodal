# -*- coding: utf-8 -*-
"""验证「模型自主决定要不要问用户」这条能力在**所有产出型任务**上都成立。

用户的需求（2026-09-21）：
    「我在生成内容时，模型并未在前端对我进行二次询问……
      我需要 ai 在生成任意内容时，包括文档、文章、代码、图片等内容时，
      都可以和文章生成一样，模型能够自主决定是否向我询问以及询问什么内容。
      当然，如果用户给出的信息很充足、或者模型用不上太多需要用户提供的信息时，
      可以不用向用户询问 —— 询问还是不询问由模型自主决定。」

排查到的两个根因（都是"只在文章路径上有"）：
  ① **代码轮**：`_TEXT_TOOL_DOCS` 里**根本没有 ask_user** ——
     而代码轮的白名单就是 `set(code_text_tools)`（= 这张表的键），
     不在表里 → 模型就算想调也会被当成无关文本清掉。
     而代码轮的提示词里还明写着「不要中途问用户」「不要反复确认、不要问他细节」，
     等于**把询问彻底禁掉**了。
  ② **工具描述与提示词都是"作文专用"**：schema 里写着"创作类任务缺关键信息时"
     并明确排除"纯技术或计算任务" —— 于是做代码、画图、做表格时从来不问。

这个脚本守三件事：
  A. ask_user 在**三条路径**（通用、长文写作、代码轮文本协议）里都拿得到；
  B. 代码轮**真的能从文本里解析出** ask_user 调用（用真实解析函数，不是看源码）；
  C. 提示词里既要有"信息不足就先问"，也要有"信息够就别问"（防止变成事事都问）。

跑法（必须用应用自己的解释器；不占端口、不联网）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_ask_user.py
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_askuser_test_")
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import tools as T                       # noqa: E402
import backend.main as M                             # noqa: E402

PASS = 0
FAIL = []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {extra}")


SRC_TOOLS = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
# ⚠️ 断"提示词里有没有这句话"时必须**去掉整行注释**再断：
# 代码注释里为了解释"以前为什么这么写"会**原文引用**旧措辞
# （例如 `# 原来这里写的是「不要中途问用户」…`），
# 不去掉的话断言会被自己的注释绊倒（这个测试第一版就踩了）。
SRC_MAIN_CODE = "\n".join(l for l in SRC_MAIN.splitlines()
                          if not l.strip().startswith("#"))


# ==========================================================================
print("\n=== 1. 工具描述要覆盖各类产出任务（不能只写作文）===")
desc = T._ASK_USER_SCHEMA["function"]["description"]
for kw in ("代码", "图片", "PPT", "表格", "文章"):
    check(f"描述里提到「{kw}」", kw in desc)
check("描述里给了各类任务该问什么（≥4 个 · 段）", desc.count("·") >= 5,
      f"{desc.count('·')} 个")
check("⚠️ 不再把「纯技术或计算任务」排除在外（这是代码/画图不问的根因）",
      "纯技术或计算任务" not in desc)
check("保留了「必须用工具问、不要在回答里用文字问」",
      "不要在回答里用文字提问" in desc)
check("保留了「什么时候别问」（防止事事都问）", "别问" in desc)
check("明确了「个数不限、可以分多轮」（2026-09-22 放宽）",
      "个数不限" in desc and "分多轮" in desc)
check("且要求「绝不重复、每个都要关键」", "绝不重复" in desc)
check("⚠️ 旧契约「只问一次」已不再出现（否则会逼模型硬做）", "只问一次" not in desc)
# ⚠️ 用户 2026-09-21 特别强调的一条：**问可以，但不能停在这一轮**
check("⚠️ 说明「提问不会结束这一轮」", "不会结束这一轮" in desc)


# ==========================================================================
print("\n=== 2. ask_user 在三条路径上都拿得到 ===")
all_names = {s["function"]["name"] for s in T.make_schemas(True, True, True)}
check("通用（聊天轮）工具列表里有 ask_user", "ask_user" in all_names)
writing_names = {s["function"]["name"] for s in T.make_schemas(True, True, True,
                                                               writing=True)}
check("长文写作模式的列表里有 ask_user", "ask_user" in writing_names)
check("⚠️ 代码轮的文本协议表里有 ask_user（原来没有 → 想调也调不动）",
      "ask_user" in M._TEXT_TOOL_DOCS)
check("ask_user 不受任何开关门控（全关也还在）",
      "ask_user" in {s["function"]["name"]
                     for s in T.make_schemas(False, False, False)})
ct_off = M._code_text_tools({})
check("代码轮白名单（开关全关）里仍有 ask_user", "ask_user" in ct_off,
      f"{sorted(ct_off)}")


# ==========================================================================
print("\n=== 3. 行为：代码轮真的能从文本里解析出 ask_user 调用 ===")
block = ('需求有点笼统，我先确认一下。\n'
         "```tool\n"
         '{"name": "ask_user", "arguments": {"questions": '
         '[{"question": "这个工具给谁用？", "header": "用途", '
         '"options": ["自己用", "交作业"]}]}}\n'
         "```\n")
allowed = set(M._code_text_tools({"code_exec_enabled": True, "web_enabled": True,
                                  "rag_enabled": True}))
calls, clean = M._split_text_tool_calls(block, allowed=allowed, bare=True)
names = [c["name"] for c in calls]
check("解析出 ask_user 调用", names == ["ask_user"], f"实得 {names}")
check("问题内容完整传过来了",
      bool(calls) and "给谁用" in str(calls[0]["arguments"]))
check("正文里的 ```tool 块被摘掉（不会漏给用户看）", "```tool" not in clean)
check("正文其余部分保留", "我先确认一下" in clean)

# 负向对照：证明白名单真的在起作用（不是"什么都放行"）
calls2, _ = M._split_text_tool_calls(
    '```tool\n{"name": "web_search", "arguments": {"query": "x"}}\n```\n',
    allowed={"ask_user"}, bare=True)
check("（对照）未在白名单里的工具不会被执行", not calls2, f"实得 {[c['name'] for c in calls2]}")

check("文本工具名 → 真实工具时原样透传（不会被改名）",
      M._map_text_tool_args("ask_user", {"questions": []})[0] == "ask_user")


# ==========================================================================
print("\n=== 4. 行为：没有弹框通道 / 用户没答 → 必须给出可读降级，不能崩 ===")
r = T.dispatch("ask_user", {"questions": [{"question": "给谁看？"}]}, [], {})
check("无 ask 通道时返回可读文案（不是异常）", isinstance(r, str) and len(r) > 10,
      r[:60])
check("  且提示模型「先按合理默认做下去」", "默认" in r)

def _fake_ask(payload):
    return [{"question": q["question"], "answer": "交作业用"} for q in payload["questions"]]

r2 = T.dispatch("ask_user", {"questions": [{"question": "给谁看？"}]}, [],
                {"ask": _fake_ask})
check("有通道时把用户答案带回来", "交作业用" in r2, r2[:60].replace("\n", " "))
check("  且带上「用户补充的信息」抬头", "用户补充的信息" in r2)

r3 = T.dispatch("ask_user", {"questions": [{"question": "给谁看？"}]}, [],
                {"ask": lambda p: []})
check("用户直接关掉弹框 → 也要说清「按默认继续」", "关掉" in r3 or "默认" in r3,
      r3[:60])

r4 = T.dispatch("ask_user", {"questions": []}, [], {})
check("空问题 → 明确报错（不是静默成功）", "错误" in r4, r4[:40])
r5 = T.dispatch("ask_user", {"questions": [{"question": ""}]}, [], {})
check("空题干 → 明确报错", "错误" in r5, r5[:40])
r6 = T.dispatch("ask_user", {"questions": [{"question": str(i)} for i in range(9)]},
                [], {"ask": _fake_ask})
# ⚠️ 2026-09-22 放宽：用户要求"每次询问的问题个数也可以不限制"。
#    以前 `qs[:4]` 会把第 5 个之后**静默丢掉**（模型以为问了、用户根本没看到）。
check("一次问 9 个 → 全部保留（不再截断成 4）", r6.count("→") == 9,
      f"{r6.count('→')} 个")

# ⚠️ 用户 2026-09-21：**问可以，但不能停在这一轮**。
# 模型拿到答复的那一刻是最容易"就地收工"的时刻，所以工具返回里必须把话说死。
check("⚠️ 拿到答复后明确告诉模型「这一轮还没结束」", "还没有结束" in r2,
      r2[-90:].replace("\n", " "))
check("⚠️ 且明确要求「接着把东西做出来」", "接着把东西做出来" in r2)
check("⚠️ 用户没答时也要求按默认继续做完（而不是停下等）",
      "继续做" in r3 or "默认" in r3)


# ==========================================================================
print("\n=== 5. 提示词：既要说「先问」，也要说「别乱问」 ===")
# 提示词没法单元测试（在 chat 接口里拼），这里断的是源码里的措辞 ——
# 改坏了会立刻发现，比"靠人记得"强。
check("有通用规则块（两个模型都注入，不在 `if not code_model_on` 里）",
      "【信息不足就先问" in SRC_MAIN)
check("  明确说这是模型的自主判断", "自主判断" in SRC_MAIN)
check("  给了各类任务该问什么（代码/图片都在）",
      "代码/程序 →" in SRC_MAIN and "图片/绘图 →" in SRC_MAIN)
check("  ⚠️ 也说了「什么时候不用问，直接做」", "什么时候不用问，直接做" in SRC_MAIN)
check("  ⚠️ 要求问完就做完，不许反复确认同一个方向",
      "不要再问同一个方向" in SRC_MAIN or "不许重复" in SRC_MAIN
      or "绝不重复" in SRC_MAIN)
check("  ⚠️⚠️ 明确「提问不会结束这一轮对话」（用户特别强调的一条）",
      "提问不会结束这一轮" in SRC_MAIN)
check("  ⚠️ 且在**通用规则与代码轮两处都写了**（弱模型那轮最容易漏）",
      SRC_MAIN_CODE.count("提问不会结束这一轮") >= 2,
      f"{SRC_MAIN_CODE.count('提问不会结束这一轮')} 处")

check("⚠️ 代码轮提示里**不再**有「不要中途问用户」", "不要中途问用户" not in SRC_MAIN_CODE)
check("⚠️ 代码轮提示里**不再**有「不要反复确认、不要问他细节」",
      "不要反复确认、不要问他细节" not in SRC_MAIN_CODE)
check("代码轮给了 ask_user 的文本协议示例（弱模型照着抄）",
      '"name": "ask_user"' in SRC_MAIN)
check("⚠️ 明确要求问题只写在 ```tool 块里（别在正文里再写一遍问句）",
      "只写在 ```tool 块里" in SRC_MAIN_CODE)
check("ask_user 是串行工具（保证「先问、后做」的顺序）",
      re.search(r"_SERIAL_TOOLS = \{[^}]*\"ask_user\"", SRC_MAIN, re.S) is not None)


print(f"\n{'='*60}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  ! " + f)
sys.exit(1 if FAIL else 0)
