# -*- coding: utf-8 -*-
"""验证「询问用户」这套新设计（用户 2026-09-22 的要求）。

用户的要求原文拆开是 7 条：
  ① 前端分两个模式：**深度询问**（问更多更深、分多轮）／**快速了解**（问到基本信息就够）；
  ② **不限制每次问的问题个数**；
  ③ 模型可以**分多轮**询问（每轮个数也不限）；
  ④ 问的必须是**无重复、关键**的问题 —— 不要一堆废话和重复内容；
  ⑤ 但也**不要为了减少询问次数而不问** —— 宁可多问一句，别理解错意图；
  ⑥ **不要问个没完没了** —— 了解到足以完成任务就够了；
  ⑦ 分清什么任务该问、什么不该问，**不要什么任务都问**。

这个脚本逐条守。跑法（不占端口、不联网）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_ask_modes.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["MM_DATA_DIR"] = tempfile.mkdtemp(prefix="mm_askmodes_")
sys.path.insert(0, ROOT)

import backend.main as M            # noqa: E402
from backend import tools as T      # noqa: E402
from backend import config as C     # noqa: E402

PASS, FAIL = 0, []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {extra}")


SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
SRC_TOOLS = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
CODE_MAIN = "\n".join(l for l in SRC_MAIN.splitlines() if not l.strip().startswith("#"))
CODE_TOOLS = "\n".join(l for l in SRC_TOOLS.splitlines() if not l.strip().startswith("#"))
HTML = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()

ASK_SCHEMA_DESC = T._ASK_USER_SCHEMA["function"]["description"]
ASK_Q_DESC = (T._ASK_USER_SCHEMA["function"]["parameters"]["properties"]["questions"]
              ["description"])


def _capture_ask(questions):
    """跑一遍 _do_ask_user，把它真正推给界面的问题抓出来。"""
    seen = {}

    def fake_ask(payload):
        seen["payload"] = payload
        return [{"question": q["question"], "answer": "知道了"} for q in payload["questions"]]

    r = T.dispatch("ask_user", {"questions": questions}, [], {"ask": fake_ask})
    return seen.get("payload") or {}, r


print("\n=== ① 两个模式：提示词要说清各自的「问到什么程度」 ===")
quick = M._ask_mode_line({"ask_mode": "quick"})
deep = M._ask_mode_line({"ask_mode": "deep"})
check("快速模式：只问最关键的几条", "1~3" in quick)
check("深度模式：问题个数不限、可以分多轮",
      "个数不限" in deep and "分多轮" in deep)
check("缺省（没配）按快速模式走", "1~3" in M._ask_mode_line({}))
check("配了乱七八糟的值也按快速走", "1~3" in M._ask_mode_line({"ask_mode": "xxx"}))
check("config 里有 ask_mode 且默认快速",
      C.DEFAULT_CONFIG.get("ask_mode") == "quick", str(C.DEFAULT_CONFIG.get("ask_mode")))

print("\n=== ② 每次问几个：**不限制**（以前超过 4 个会被静默丢掉）===")
qs8 = [{"question": f"问题{i}？", "header": f"H{i}", "options": ["A", "B"]} for i in range(8)]
payload, ret = _capture_ask(qs8)
check("8 个问题全部推到界面（一个都不丢）",
      len(payload.get("questions") or []) == 8,
      f"实得 {len(payload.get('questions') or [])} 个")
qs30 = [{"question": f"问题{i}？"} for i in range(30)]
payload2, _ = _capture_ask(qs30)
n2 = len(payload2.get("questions") or [])
check("只保留「防发疯」的兜底上限（20），不再是 4", n2 == T._ASK_HARD_CAP and n2 > 4,
      f"实得 {n2}，上限常量 {T._ASK_HARD_CAP}")
check("兜底上限远高于真实需要（≥10）", T._ASK_HARD_CAP >= 10)

print("\n=== ③ 多轮询问：答复回来后要允许再问一轮 ===")
check("明确允许「再调用一次 ask_user 问一轮」",
      "再调用一次 ask_user" in ret or "再问一轮" in ret,
      ret[-120:].replace("\n", " "))
check("措辞里不再出现「只问一次」", "只问一次" not in CODE_TOOLS)
check("也不再说「一次做完，不要再重复问同样的问题」那种硬话",
      "基于这些信息一次做完" not in CODE_TOOLS)

print("\n=== ④ 不许重复、不许凑数 ===")
check("schema 写明「绝不重复」", "绝不重复" in ASK_SCHEMA_DESC)
check("schema 写明「每个都要关键」", "每个都要关键" in ASK_SCHEMA_DESC or "关键问题" in ASK_SCHEMA_DESC)
check("工具返回里写死「已经答过的一个字都不要再问」", "一个字都不要再问" in ret)
check("提示词里也有「绝不重复」", "绝不重复" in CODE_MAIN)
check("问题列表说明要求按重要性排序、别凑数",
      "按重要性" in ASK_Q_DESC and "别凑数" in ASK_Q_DESC, ASK_Q_DESC[:60])

print("\n=== ⑤ 不许为了少打扰而跳过关键问题 ===")
check("提示词点明这条（用户特别强调）",
      "不要为了「少打扰」就跳过关键问题" in CODE_MAIN or "不要为了少打扰" in CODE_MAIN)
check("并给了理由：方向问错代价更大", "方向问错了" in CODE_MAIN)

print("\n=== ⑥ 也别没完没了 ===")
check("提示词写了「别没完没了」", "别没完没了" in CODE_MAIN)
check("并写明「该知道的都知道了就动手」", "该知道的都知道了就动手做" in CODE_MAIN)

print("\n=== ⑦ 分清什么该问、什么不该问 ===")
# ⚠️ 实测踩到：模型会把问题**写在正文里**（"好的，请问…？"），
#    用户看到的是一个问句、没有弹框可点 —— 这一轮等于白结束。deep 模式下更容易犯。
check("⚠️ 写死了「想问题就必须走 ask_user，正文里写问句不算提问」",
      "就必须走 ask_user 工具" in CODE_MAIN and "不算提问" in CODE_MAIN)
for kw in ("打招呼", "闲聊", "查资料", "算个数"):
    check(f"「不用问」清单里列了「{kw}」", kw in CODE_MAIN)
check("schema 里也列了不该问的几类", "什么时候**别问**" in ASK_SCHEMA_DESC)
check("提示词提醒「别什么任务都问」", "别什么任务都问" in CODE_MAIN)
check("旧的「最多 4 个问题」表述已彻底移除",
      "最多 4 个" not in CODE_TOOLS and "最多 4 个" not in CODE_MAIN and "1-4 个" not in CODE_TOOLS)

print("\n=== 弹框里要能看出当前是哪个模式 ===")
hint = {}


def _grab(payload):
    hint["v"] = payload.get("hint")
    return []


_ = T.dispatch("ask_user", {"questions": [{"question": "x?"}]}, [],
               {"ask": _grab, "ask_mode": "deep"})
check("深度模式：弹框提示写明「深度询问」", "深度询问" in (hint.get("v") or ""), str(hint.get("v"))[:50])
_ = T.dispatch("ask_user", {"questions": [{"question": "x?"}]}, [],
               {"ask": _grab, "ask_mode": "quick"})
check("快速模式：弹框提示写明「快速了解」", "快速了解" in (hint.get("v") or ""), str(hint.get("v"))[:50])

print("\n=== 前端：合并成一个「询问方式」按钮（用户 2026-09-22 要求）===")
check("⚠️ 已不再并排两个按钮（data-askmode 痕迹清干净）", "data-askmode" not in HTML,
      "index.html 里仍有 data-askmode")
check("有一个按钮，id=askModeBtn", 'id="askModeBtn"' in HTML)
check("按钮上写着「询问方式」", "询问方式" in HTML)
btn_lines = [l for l in HTML.splitlines() if "askModeBtn" in l and "<button" in l]
check("⚠️ 这个按钮**不带 data-cfg**（否则会被当成布尔开关，与 saveToggles 打架）",
      bool(btn_lines) and all("data-cfg" not in l for l in btn_lines))
check("按钮有默认文案（未读配置前也不会空着）", "询问方式：快速" in HTML)
check("app.js 用 ASK_MODES 描述两种模式", "ASK_MODES" in JS)
check("app.js 把当前选择写进按钮文字", "askModeLabel" in JS and "textContent" in JS)
check("app.js 有选择面板（点开切换，不用挤在两个按钮里）", "showAskModePicker" in JS)
check("⚠️ 模式的唯一事实来源是变量（不再从 DOM 反推）",
      "let askMode" in JS and '.pill.askmode' not in JS)
check("读配置时会把模式刷到界面上", "setAskMode(c.ask_mode" in JS)
check("存开关时会带上 ask_mode（两处状态不错步）", "ask_mode:" in JS)
check("切模式后给用户反馈", "深度询问" in JS and "快速了解" in JS)

print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  ! " + f)
sys.exit(1 if FAIL else 0)
