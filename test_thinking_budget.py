# -*- coding: utf-8 -*-
"""回归测试：思考吃光配额时，重试必须"先腾窗口"，最坏情况也不能给空白。

背景（2026-09-25 用户反馈）：
    界面上出现「模型把输出空间都花在思考上了，没能写出正文」。
    实测根因是**窗口被提示词吃掉 60%**：
      · 全开 33 个工具的 schema = 32280 字符，实测吃掉 14706 token
      · num_ctx = 24576 → 只剩不到 1 万给"思考 + 正文"
      · 而旧的重试只把 max_tokens 加到 16384 —— **远超剩余窗口**，
        必然再截断一次 → 白转两轮 → 最后还是没正文。

跑法：python test_thinking_budget.py
覆盖两个场景：
  A. 窗口被工具吃掉、但砍掉工具就能腾出来 → 重试应当"精简工具 + 按真实窗口给额度"
  B. 窗口本来就快满了（提示词 24000/24576）→ **别白试**，直接走兜底
两个场景都必须满足：再差也要把思考内容交付给用户，不能留空白。
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_budget_")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
CFG.update({"web_enabled": False, "rag_enabled": False,
            "memory_enabled": False, "auto_memorize": False,
            "code_auto_route": False, "code_model": ""})
json.dump(CFG, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import main as M          # noqa: E402
from backend.main import ChatRequest   # noqa: E402

NUM_CTX = int(CFG.get("num_ctx") or 8192)
SAFETY = 512
PASS = FAIL = 0

# 精简工具到底能省多少 token —— **不能写死**。
# 它会随工具 schema 变化：2026-09-26 给三个生成工具补了"丰富度要求"后，
# saving 从 11706 涨到一万三，而这里原来是硬编码的（A 用 3000 / B 用 10782），
# 于是 B2 判出"额度超过了剩余窗口" —— **假警报**，应用的估算其实是对的。
# 这里跟应用**用同一套度量**（同两个 make_schemas 调用 + 同一个 _est_tokens）。
# ⚠️ 两个调用都要带 ask_mode：ask_user 的**工具描述**会随模式变长，
#    漏传就差 156 token，于是精算出的"省了多少"和应用的账对不上（实测踩到）。
from backend import tools as _TM                                # noqa: E402

_ASK = str(CFG.get("ask_mode") or "quick")
# 测试用的用户消息不触发写作/办公模式，所以 writing/office 都是 False（同应用）
_FULL_SCHEMAS = _TM.make_schemas(CFG.get("web_enabled", False),
                                 CFG.get("rag_enabled", False),
                                 CFG.get("code_exec_enabled", False),
                                 writing=False, office=False, ask_mode=_ASK)
_TRIM_SCHEMAS = _TM.make_schemas(CFG.get("web_enabled", False),
                                 CFG.get("rag_enabled", False), False,
                                 writing=True, ask_mode=_ASK)
FREED = max(0, M._est_tokens(json.dumps(_FULL_SCHEMAS, ensure_ascii=False))
            - M._est_tokens(json.dumps(_TRIM_SCHEMAS, ensure_ascii=False)))


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


TH1 = "【第一次思考】先想想要不要回答这个问题，可能还得再确认一下用户到底想要什么。"
TH2 = "【第二次思考】换个方向再想想，也许应该直接给出结论而不是继续推演。"


class FakeResp:
    def __init__(self, chunks):
        self._lines = [json.dumps(c, ensure_ascii=False).encode("utf-8")
                       for c in chunks]

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


def make_fake(prompt_tokens_seq):
    """每次调用都"只思考、不写正文"，并撞到上限（done_reason=length）。

    prompt_tokens_seq：每次调用**Ollama 报的提示词 token 数**（按序取，用完取最后一个）。
    """
    state = {"n": 0, "tools": [], "max_tokens": [], "tool_names": []}

    def fake_chat(messages, model=None, stream=True,
                  images_base64=None, params=None, tools=None):
        i = state["n"]
        state["n"] += 1
        state["tools"].append(len(tools or []))
        # 记下**名字**（不只是个数）——场景 C 要确认 make_pptx 还在不在
        state["tool_names"].append([(t.get("function") or {}).get("name")
                                    for t in (tools or []) if isinstance(t, dict)])
        state["max_tokens"].append((params or {}).get("max_tokens"))
        pt = prompt_tokens_seq[min(i, len(prompt_tokens_seq) - 1)]
        return FakeResp([
            {"message": {"thinking": TH1 if i == 0 else TH2}, "done": False},
            {"done": True, "done_reason": "length", "eval_count": 800,
             "prompt_eval_count": pt},
        ])

    return fake_chat, state


async def run_once(prompt_tokens_seq, user="帮我看看这个问题"):
    fake, state = make_fake(prompt_tokens_seq)
    M.client.chat = fake
    req = ChatRequest(messages=[{"role": "user", "content": user}],
                      session_id="test-budget-%d" % id(state))
    resp = await M.chat(req)
    events = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        for line in chunk.split("\n"):
            if line.strip():
                events.append(json.loads(line))
    return events, state


print("=" * 62)
print("思考吃光配额 → 重试腾窗口 + 兜底交付（num_ctx=%d）" % NUM_CTX)
print("=" * 62)

# ---------------- 场景 A：工具吃掉了窗口，但砍掉就能腾出来 ----------------
print("\n【场景 A】提示词 14706（工具占大头，精简能省 %d），重试时可腾空间" % FREED)
events, state = asyncio.run(run_once([14706, 14706 - FREED]))
done = next((e for e in events if e.get("done")), {})
resets = [i for i, e in enumerate(events)
          if e.get("message", {}).get("thinking_reset")]
print("   每次调用：tools=%s  max_tokens=%s" % (state["tools"], state["max_tokens"]))
# 第 2 次调用时 Ollama 实报的提示词 token（= 原提示词 − 实测省下的）
pt2 = 14706 - FREED
room2 = max(1024, NUM_CTX - pt2 - SAFETY)
check("A1 第一次带全部工具（对照）", state["tools"][0] > 5,
      "tools=%d" % state["tools"][0])
check("A2 重试时工具被精简（腾窗口）",
      len(state["tools"]) > 1 and state["tools"][1] < state["tools"][0])
check("A3 重试的额度落在真实剩余窗口内（不再顶到窗口外）",
      len(state["max_tokens"]) > 1 and state["max_tokens"][1] <= room2,
      "max_tokens=%s  可用=%d" % (state["max_tokens"][1:], room2))
check("A4 额度确实加大了（这次重试有意义）",
      len(state["max_tokens"]) > 1 and state["max_tokens"][1] > state["max_tokens"][0])
check("A5 发出了 thinking_reset（清掉上一轮思考）", len(resets) >= 1)
check("A6 最坏情况不给空白：正文非空", bool((done.get("text") or "").strip()),
      "正文 %d 字" % len((done.get("text") or "")))
check("A7 兜底内容有明确标注", "思考过程" in (done.get("text") or ""))
# 2026-09-25：天花板从 16384 提到 24576（用户要求"取消篇幅限制"）之后，
# 重试**能再涨一轮**（16384 → 21064），所以第二次重试不再被跳过 —— 这是要的效果。
# 断言改成：额度只涨不超窗、且最多重试 _MAX_EMPTY_RETRIES 次。
_room_A = max(1024, NUM_CTX - pt2 - SAFETY)
check("A8 每次加大的额度都不超过真实剩余窗口",
      all(mt <= _room_A for mt in state["max_tokens"][1:]),
      "max_tokens=%s  可用=%d" % (state["max_tokens"], _room_A))
check("A9 最多重试 2 次（共 3 次调用），不会无限重试",
      state["n"] == 3, "调用次数=%d" % state["n"])

# ---------------- 场景 B：连历史都很大 → 砍完工具仍腾不出多少，别白试 ----------------
# ⚠️⚠️ 2026-09-26 改成**按窗口推导**（原来写死 24000，是个"当时刚好成立"的魔数）：
#   这个场景要成立，必须让"第二次之后的剩余窗口"小到**涨不上去**：
#       room = NUM_CTX − p2 − SAFETY ≤ 16384（第一次重试后的额度）⇒ p2 ≥ NUM_CTX − 16896
#   写死 24000 之后：num_ctx 从 24576 提到 28672，p2 = 24000−FREED ≈ 1.1 万
#   → 剩余窗口变成 16851 > 16384 → 应用**有理由**再试第三轮 → 断言成了假失败
#   （不是代码错，是测试的假设过时 —— 同类"写死数字"的坑今天已经踩过三次）。
_P2B = NUM_CTX - 16896 + 1500          # 留 1500 余量，确保"腾完也涨不上去"
_P1B = _P2B + FREED                    # 第一次比第二次多出的那部分 = 精简工具省下的窗口
print("\n【场景 B】提示词 %d（历史+工具都很大），腾完空间后就不再白试" % _P1B)
print("   （按窗口推导：num_ctx=%d，第二次提示词 %d → 剩余 %d ≤ 16384 才该停）"
      % (NUM_CTX, _P2B, NUM_CTX - _P2B - SAFETY))
events_b, state_b = asyncio.run(run_once([_P1B, _P2B]))
done_b = next((e for e in events_b if e.get("done")), {})
print("   每次调用：tools=%s  max_tokens=%s" % (state_b["tools"], state_b["max_tokens"]))
check("B1 只重试了有意义的那一次（共 2 次调用，不试第三轮）", state_b["n"] == 2,
      "调用次数=%d（剩余窗口 %d）" % (state_b["n"], NUM_CTX - _P2B - SAFETY))
check("B2 额度没有超过真实剩余窗口",
      state_b["max_tokens"][-1] <= max(1024, NUM_CTX - _P2B - SAFETY),
      "max_tokens=%s" % state_b["max_tokens"])
check("B3 仍然把思考交付出来（正文非空）",
      bool((done_b.get("text") or "").strip()),
      "正文 %d 字" % len((done_b.get("text") or "")))
check("B4 兜底内容有明确标注", "思考过程" in (done_b.get("text") or ""))

# ---------------- 场景 C：办公任务重试时**不能把生成工具砍掉** ----------------
# ⚠️ 这是实测踩到的坑（2026-09-26）：重试原来统一切成"写作精简集"，
#    而那个集合里**没有 make_pptx**。于是"首次尝试把额度烧在思考上"之后，
#    重试这一轮模型根本没法生成文件 —— 用户要 PPT，最后只拿到一段文字。
#    （端到端实测：工具=[]、一个产物都没有。）
print("\n【场景 C】办公任务（做 PPT）重试时必须保住生成工具")
_OFFICE_Q = "帮我做一份建设方案PPT，要求内容充实、专业"
events_c, state_c = asyncio.run(run_once([14706, 14706 - FREED], user=_OFFICE_Q))
print("   每次调用工具数：%s" % state_c["tools"])
print("   第一次的工具：%s" % (state_c["tool_names"][0] if state_c["tool_names"] else []))
print("   重试后的工具：%s" % (state_c["tool_names"][-1] if state_c["tool_names"] else []))
check("C0 这句话确实被判定为办公任务", M._is_office_task(_OFFICE_Q), _OFFICE_Q)
check("C1 第一次就带上了 make_pptx",
      "make_pptx" in (state_c["tool_names"][0] if state_c["tool_names"] else []))
check("C2 ★ 重试后**仍然**有 make_pptx（不许砍掉生成工具）",
      "make_pptx" in (state_c["tool_names"][-1] if state_c["tool_names"] else []),
      "重试后的工具=%s" % (state_c["tool_names"][-1] if state_c["tool_names"] else []))
check("C3 重试确实精简了（腾了窗口）",
      len(state_c["tools"]) > 1 and state_c["tools"][-1] < state_c["tools"][0],
      "工具数=%s" % state_c["tools"])

print("\n" + "=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
