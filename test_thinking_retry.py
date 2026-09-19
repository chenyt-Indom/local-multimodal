# -*- coding: utf-8 -*-
"""思考过程重复 bug 的回归测试：重试时必须把上一轮的思考作废。

背景（2026-09-19 用户反馈"模型思考过程有重复内容"）：
    qwen3-vl 偶尔会在思考里原地打转（同一个例子反复推敲），把 num_predict
    配额烧光、正文一个字都没写出来。这时应用会"加长输出上限重试"。
    重试是**从头重新生成**，思考自然也会重新来一遍 ——
    但旧代码只把新思考**追加**在旧思考后面（后端 final_thinking 不清零、
    前端面板也不清空），用户看到的就是"同一个思路说了两遍"。

跑法：python test_thinking_retry.py
断言：
  ① 重试路径确实被走到（出现"正在自动加长输出上限重试"提示）
  ② 重试前先发出 thinking_reset，且它排在第二次思考之前
  ③ 重置之后推给前端的思考里**不含第一次的思考**
  ④ 结束事件的 thinking 只等于最后一次的思考（不再叠加）
  ⑤ 重试确实把 max_tokens 加倍了
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

# 临时数据目录 + 拷 config（不拷会走 DEFAULT_CONFIG，得到假结论）
TMP = tempfile.mkdtemp(prefix="mm_think_")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
# 关掉会**额外调用模型**的功能，保证脚本里的桩只服务本轮对话
CFG.update({"web_enabled": False, "rag_enabled": False,
            "memory_enabled": False, "auto_memorize": False,
            "code_auto_route": False, "code_model": ""})
json.dump(CFG, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import main as M          # noqa: E402
from backend.main import ChatRequest   # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


THINK_1 = "【第一次思考】可能还是不够，再想想：什么字最不愿意看到？"
THINK_2 = "【第二次思考】换个方向：什么门永远关不上。"


class FakeResp:
    """冒充 requests 的流式响应：只要 iter_lines / close 两个方法。"""

    def __init__(self, chunks):
        self._lines = [json.dumps(c, ensure_ascii=False).encode("utf-8")
                       for c in chunks]

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


def make_fake_chat():
    """第 1 次调用：思考烧完配额、正文为空、done_reason=length（触发重试）
       第 2 次调用：思考 + 正文，正常结束。
       之后的任何调用（后台任务等）：给个无害的短回答。"""
    state = {"n": 0, "params_seen": []}

    def fake_chat(messages, model=None, stream=True,
                  images_base64=None, params=None, tools=None):
        state["n"] += 1
        state["params_seen"].append(dict(params or {}))
        if state["n"] == 1:
            return FakeResp([
                {"message": {"thinking": THINK_1}, "done": False},
                {"message": {"content": ""}, "done": False},
                {"done": True, "done_reason": "length", "eval_count": 100},
            ])
        if state["n"] == 2:
            return FakeResp([
                {"message": {"thinking": THINK_2}, "done": False},
                {"message": {"content": "这是最终答案。"}, "done": False},
                {"done": True, "done_reason": "stop", "eval_count": 30},
            ])
        return FakeResp([
            {"message": {"content": "（后台任务）无"}, "done": False},
            {"done": True, "done_reason": "stop", "eval_count": 5},
        ])

    return fake_chat, state


async def run_once():
    fake, state = make_fake_chat()
    M.client.chat = fake
    req = ChatRequest(messages=[{"role": "user", "content": "继续"}],
                      session_id="test-thinking-retry")
    resp = await M.chat(req)
    events = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        for line in chunk.split("\n"):
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events, state


def main():
    print("=" * 62)
    print("思考重复 bug 回归测试（临时数据目录：%s）" % TMP)
    print("=" * 62)

    events, state = asyncio.run(run_once())

    notes = [e["note"] for e in events if e.get("note")]
    think_parts = [e["message"]["thinking"] for e in events
                   if e.get("message", {}).get("thinking")]
    reset_idx = [i for i, e in enumerate(events)
                 if e.get("message", {}).get("thinking_reset")]
    think2_idx = [i for i, e in enumerate(events)
                  if e.get("message", {}).get("thinking") == THINK_2]
    done = next((e for e in events if e.get("done")), {})

    print("\n--- 事件流（前 12 条）---")
    for e in events[:12]:
        print("   ", json.dumps(e, ensure_ascii=False)[:110])

    print("\n--- 断言 ---")
    check("① 走到了「加长上限重试」分支",
          any("重试" in n for n in notes),
          notes[:2])
    check("② 重试前发出了 thinking_reset",
          len(reset_idx) == 1, "reset 位置=%s" % reset_idx)
    check("③ reset 排在第二次思考之前",
          bool(reset_idx) and bool(think2_idx) and reset_idx[0] < think2_idx[0],
          "reset=%s think2=%s" % (reset_idx, think2_idx))
    # 重置事件之后，还推给前端的思考增量（按事件顺序取，不能用 think_parts 的下标）
    after = "".join(e["message"]["thinking"] for i, e in enumerate(events)
                    if reset_idx and i > reset_idx[0]
                    and e.get("message", {}).get("thinking"))
    before = "".join(e["message"]["thinking"] for i, e in enumerate(events)
                     if reset_idx and i < reset_idx[0]
                     and e.get("message", {}).get("thinking"))
    check("④ 重置之前确实推过第一次的思考（场景成立）",
          before == THINK_1, "before=%r" % before)
    check("⑤ 重置之后不再出现第一次的思考",
          bool(after) and THINK_1 not in after, "after=%r" % after[:60])
    check("⑥ 结束事件的 thinking 只剩最后一次（没有叠加）",
          done.get("thinking") == THINK_2, "done.thinking=%r" % (done.get("thinking"),))
    check("⑦ 结束事件的正文正确",
          done.get("text", "").strip().endswith("这是最终答案。"),
          "done.text=%r" % (done.get("text"),))
    tokens = [p.get("max_tokens") for p in state["params_seen"][:2]]
    check("⑧ 重试把 max_tokens 加倍了",
          len(tokens) == 2 and tokens[1] > tokens[0], "max_tokens=%s" % tokens)

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 62)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
