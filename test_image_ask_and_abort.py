# -*- coding: utf-8 -*-
"""守住用户 2026-09-22 08:00 报的两个问题。

问题一：「生成一张图片，关于狐狸」时模型**没问就直接画**。
  用户问得很准：是文生图模型不询问，还是大模型不询问？
  → **文生图（本地 SD）根本没有对话/提问通道**，它只有"一段文本 → 一张图"。
    问不问完全由**大语言模型**决定。实测那次模型的思考里写着
    「没有给出特定要求 → 提供通用但高质量的狐狸形象」——
    被「能合理默认的别问」那一条放过去了。而画图风格选错**要重画**（几十秒）。
    修法：把"模糊的画图需求"钉死成**必须先问一轮**（`_ask_rules()` + 工具描述）。

问题二：问「红腹锦鸡长什么样」，图库里**混进一张上一条要的狐狸图**。
  根因：上一轮被「■ 终止」后，历史里只留下一条**没人回答的用户消息**；
  下一轮模型把它当成未完成的需求接着做。
  ⚠️ 已复现：把这种历史直接发过去，模型当场又调了一次 generate_image，
  而且**完全没回答这一轮真正的问题**。
  修法：① 前端终止时**总是**往历史里写一条说明"这条需求作废"的助手消息；
        ② 后端再加一道：发现"两条连续的用户消息"就往本轮用户消息里插一句提醒。

跑法：python test_image_ask_and_abort.py   （必须用 Python 3.14）
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_imgask_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import config as C     # noqa: E402
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


CFG = C.load_config()
SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()

print("=" * 64)
print("① 画图前要先问：规则确实进了系统提示（而且能单测了）")
rules = M._ask_rules(CFG)
check("_ask_rules() 已经抽成独立函数（原来是内联的，测不到）",
      callable(getattr(M, "_ask_rules", None)))
check("规则里明确写了「画图是个例外，宁问勿猜」", "画图/生成图片是个例外" in rules)
check("要求先把风格/用途/氛围/尺寸问清", "先 ask_user 问一轮" in rules
      and "风格" in rules and "用途" in rules)
check("讲了理由（画一次几十秒、错了要重画）", "几十秒" in rules and "重画" in rules)
check("⚠️ 已经说清风格的**不许再问**（避免退化成反复确认）",
      "一个字都别问" in rules)
check("⚠️ 只问一轮、答复到手就得开画", "只问一轮" in rules and "必须动手画" in rules)
check("接口里确实用了这个函数（抽了却没人调＝白搭）",
      "sys_prompt += _ask_rules(cfg)" in SRC_MAIN)
check("原本那条「能合理默认的别问」还在（没被删掉）", "别什么任务都问" in rules)

print()
print("=" * 64)
print("② 工具描述里也有（模型最先看的是 schema）")
sch = T.make_schemas(True, True, True)
gen = [s for s in sch if (s.get("function") or {}).get("name") == "generate_image"]
check("generate_image schema 存在", len(gen) == 1)
desc = (gen[0]["function"].get("description") or "") if gen else ""
check("描述里点了「先问再画」", "先调 ask_user 问一轮再画" in desc, desc[:80])
check("描述里讲了原因（几十秒 / 重画）", "几十秒" in desc and "重画" in desc)
check("描述里带了「说清了就别再问」（防过度询问）", "不要再问" in desc)
check("原来的「生成后直接展示给用户」还在", "直接展示给用户" in desc)

print()
print("=" * 64)
print("③ 上一条没人回答时，后端会提醒模型「别接着做」")
sys_msg = {"role": "system", "content": "SYS"}


def build(hist, rag=""):
    return M._attach_live_ctx([sys_msg] + hist, mem_ctx="", rag_ctx=rag)


ok_hist = [{"role": "user", "content": "生成一张图片，关于狐狸"},
           {"role": "assistant", "content": "〔已终止〕…作废…"},
           {"role": "user", "content": "红腹锦鸡长什么样？"}]
out = build(ok_hist)
check("正常历史：不插提醒（不打扰）",
      "上一条" not in out[-1]["content"], out[-1]["content"][:50])

dangling = [{"role": "user", "content": "生成一张图片，关于狐狸"},
            {"role": "user", "content": "红腹锦鸡长什么样？"}]
out2 = build(dangling)
check("⚠️ 两条连续的用户消息 → 插了提醒",
      "用户消息没有得到你的回复" in out2[-1]["content"],
      out2[-1]["content"][:70])
check("提醒说清了三件事：作废、别合并、只答本轮",
      all(k in out2[-1]["content"] for k in ("作废", "不要和这一条合并", "只回答本轮")))
check("⚠️ 没有另插一条 system 消息（qwen 模板只用第一条，会被静默丢弃）",
      sum(1 for m in out2 if m.get("role") == "system") == 1)
check("原文的问题一个字没少", "红腹锦鸡长什么样？" in out2[-1]["content"])
check("原来的历史 dict 没被就地改动", dangling[0]["content"] == "生成一张图片，关于狐狸")

first_turn = [{"role": "user", "content": "你好"}]
check("只有一条用户消息：不插提醒",
      "上一条" not in build(first_turn)[-1]["content"])

print()
print("=" * 64)
print("④ 前端：点终止时**一定**留下助手消息（历史里不留悬空提问）")
check("终止分支里无条件 push 一条 assistant",
      "history.push({ role: \"assistant\"," in JS and "stopMark" in JS)
check("这条消息对模型说清了「作废、别在新一轮接着做」",
      "上面那条需求就此作废" in JS and "不要在新的一轮里接着做" in JS)
check("⚠️ 就算一个字都没生成也要写（原来的 if (answer) 会漏掉）",
      'content: (answer ? answer + "\\n\\n" : "") + stopMark' in JS)
check("显示与落盘用同一段文本（刷新后不会两处不一致）",
      "answerBubble.textContent = answer ? answer + \"\\n\\n\" + stopMark : stopMark;" in JS)
check("落盘照旧执行（persistSession 还在）", "persistSession()" in JS)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 64)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
