# -*- coding: utf-8 -*-
"""守住「生成到一半任务断了 / 模型这次没有输出内容」这条修复（2026-09-27）。

用户原话：
  · "生成东西时有时候会中途中断任务"
  · 提示的是「⚠️ 模型这次没有输出内容，请再试一次或换个问法」
  · "这个问题出现在代码生成中，不知道其他功能会不会也有这个问题"

端到端实测挖出**两条独立的原因**，各自修掉并在这里守住：

① 【工具轮次用满就静默退场】—— 影响**所有**多步任务（不只代码）
   写代码/做项目天生是"一轮写一个文件 + 一轮跑一次"，很容易吃满
   `MAX_TOOL_ROUNDS = 10` 轮。原来 `for _round in range(10)` 跑完就**直接往下走**，
   模型一个字都没交代 → 前端只能兜一句通用的"没有输出内容"。
   现在补了 `for … else:` 收尾轮：不带工具，要它用文字交代进度。

② 【代码模型被长度上限截断就判失败】—— 只影响代码生成
   代码模型单次最多吐 `code_max_tokens`（默认 8192 ≈ 两三百行），稍大的文件必撞上限
   （`done_reason == "length"`）。原来**单跑一次、撞了就返回"没有输出内容"**。
   现在把已写出的部分回喂，让它从断点自动续写（最多 `_CODE_CONTINUE_MAX` 次），
   并把"续写到头还是截断"如实标注 —— 绝不让半成品冒充成品。

跑法：python test_code_continue.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_codecont_")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
# 关掉会**额外调用模型**的功能，保证桩只服务被测的那条路径
CFG.update({"web_enabled": False, "rag_enabled": False,
            "memory_enabled": False, "auto_memorize": False,
            "code_auto_route": False, "code_model": ""})
json.dump(CFG, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import main as M            # noqa: E402
from backend import tools as T           # noqa: E402
from backend import workspace as WS      # noqa: E402
from backend.main import ChatRequest     # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name + (("   " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  [!!] " + name + (("   → " + str(detail)) if detail else ""))


class FakeResp:
    """冒充 requests 的流式响应（只要 iter_lines / close）。"""

    def __init__(self, chunks):
        self._lines = [json.dumps(c, ensure_ascii=False).encode("utf-8")
                       for c in chunks]

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


class FakeWS:
    """替掉真实工作区：只记录写入，不碰磁盘。"""

    def __init__(self):
        self.files = {}
        self.writes = 0

    def safe_rel(self, rel):
        return str(rel or "").replace("\\", "/")

    def read_text(self, rel):
        return {"ok": True, "text": self.files.get(rel, "")}

    def stream_write(self, rel, text):
        self.files[rel] = text
        return {"ok": True}

    def write_text(self, rel, text, by="ai"):
        self.files[rel] = text
        self.writes += 1
        return {"ok": True}

    def active_project(self):
        return "testproj"


# =====================================================================
#  ① 代码模型被截断 → 必须自动续写，并把两段拼成一份完整文件
# =====================================================================
PART1 = "def add(a, b):\n    return a + b\n\n# ...（被截断）"
PART2 = "def sub(a, b):\n    return a - b\n"


def run_write_code(script, extra=None):
    """用给定的"模型脚本"跑一次 _do_write_code。

    script: [(正文, done_reason), ...] 依次作为每次模型调用的响应。

    ⚠️ `_do_write_code` 里是**函数内局部导入** `from . import workspace as _ws`，
    所以不能改 `tools._ws`（根本没有这个属性）—— 要改 `backend.workspace` 自己
    的属性（同一个模块对象）。
    """
    ws = FakeWS()
    names = ("safe_rel", "read_text", "stream_write", "write_text", "active_project")
    saved = {n: getattr(WS, n) for n in names}
    for n in names:
        setattr(WS, n, getattr(ws, n))
    old_chat = M.client.chat
    calls = {"n": 0, "prompts": []}

    def fake_chat(messages, model=None, stream=True,
                  images_base64=None, params=None, tools=None):
        calls["n"] += 1
        calls["prompts"].append(json.dumps(messages, ensure_ascii=False))
        i = min(calls["n"] - 1, len(script) - 1)
        text, reason = script[i]
        return FakeResp([
            {"message": {"content": text}, "done": False},
            {"done": True, "done_reason": reason, "eval_count": 10},
        ])

    M.client.chat = fake_chat
    try:
        args = {"rel": "demo.py", "instruction": "写两个函数 add / sub", "run": False}
        if extra:
            args.update(extra)
        out = T._do_write_code(args, ui_events=[], context=None)
    finally:
        for n in names:
            setattr(WS, n, saved[n])
        M.client.chat = old_chat
    return out, ws, calls


def test_code_continue():
    print("=" * 66)
    print("① 代码被截断 → 自动续写（原来直接判失败）")
    print("=" * 66)

    out, ws, calls = run_write_code([(PART1, "length"), (PART2, "stop")])
    code = ws.files.get("demo.py", "")
    check("续写被触发了（模型被调用了 2 次）", calls["n"] == 2, "调用了 %d 次" % calls["n"])
    check("两次输出被拼在同一份文件里",
          PART1.strip() in code and PART2.strip() in code, "文件 %d 字" % len(code))
    check("续写的提示里明确要求「从断点往后接着写」",
          "从断点往后接着写" in calls["prompts"][-1])
    check("返回给大脑的文字**不再**是「没有输出内容」",
          "没有输出内容" not in out, out.splitlines()[0][:60] if out else "")
    check("返回里报了写入字数", "已把代码写进" in out)

    # 正常写完（没被截断）→ 不该多调一次
    out2, ws2, calls2 = run_write_code([("print(1)\n", "stop")])
    check("没被截断时不多调用（只 1 次）", calls2["n"] == 1, "调用了 %d 次" % calls2["n"])
    check("没被截断时也不带残缺警告", "可能还不完整" not in out2)

    # 一直截断 → 必须如实告知"可能不完整"，别把半成品当成品
    out3, ws3, calls3 = run_write_code([(PART1, "length")])
    check("反复截断后如实标注「可能还不完整」", "可能还不完整" in out3, out3.strip()[-40:])
    check("反复截断时续写次数受上限保护",
          calls3["n"] == 1 + T._CODE_CONTINUE_MAX, "调用了 %d 次" % calls3["n"])

    # 一个字都没有 → 保留原来的失败提示（该报错还是要报错）
    out4, ws4, calls4 = run_write_code([("", "stop")])
    check("模型真的没输出时仍然如实报错", "没有输出内容" in out4, out4[:40])


# =====================================================================
#  ② 工具轮次用满 → 必须补一个收尾轮，而不是静默退场
# =====================================================================
ROUNDS = M.MAX_TOOL_ROUNDS
WRAP = "已完成：demo.py 里写了 add/sub 两个函数。还差：没有写测试。下一步：补单元测试。"


def make_rounds_chat():
    """前 ROUNDS 轮全部只调 get_time（不产出正文）→ 触发轮次耗尽；
    收尾轮（tools=None）返回一段总结。"""
    state = {"n": 0, "wrap_called": 0, "blocks": []}

    def fake_chat(messages, model=None, stream=True,
                  images_base64=None, params=None, tools=None):
        state["n"] += 1
        state["blocks"].append(tools)
        if tools is None:                     # 收尾轮：不带工具
            state["wrap_called"] += 1
            return FakeResp([
                {"message": {"content": WRAP}, "done": False},
                {"done": True, "done_reason": "stop", "eval_count": 20},
            ])
        return FakeResp([
            {"message": {"content": "", "tool_calls": [
                {"function": {"name": "get_time", "arguments": {}}}]}, "done": False},
            {"done": True, "done_reason": "stop", "eval_count": 5},
        ])

    return fake_chat, state


async def run_chat_once():
    fake, state = make_rounds_chat()
    old = M.client.chat
    M.client.chat = fake
    try:
        req = ChatRequest(messages=[{"role": "user", "content": "帮我把这个项目做完"}],
                          session_id="test-code-continue")
        resp = await M.chat(req)
        events = []
        async for chunk in resp.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            for line in chunk.split("\n"):
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    finally:
        M.client.chat = old
    return events, state


def test_rounds_exhausted():
    print()
    print("=" * 66)
    print("② 工具轮次用满 → 收尾轮（原来静默退场，前端只能说「没有输出内容」）")
    print("=" * 66)

    events, state = asyncio.run(run_chat_once())
    notes = [e["note"] for e in events if e.get("note")]
    done = next((e for e in events if e.get("done")), {})
    text = (done.get("text") or "")

    tool_rounds = sum(1 for b in state["blocks"] if b is not None)
    check("确实跑满了工具轮次上限", tool_rounds == ROUNDS, "%d 轮" % tool_rounds)
    check("轮次用满后**补了收尾轮**（不带工具）", state["wrap_called"] == 1,
          "收尾轮 %d 次" % state["wrap_called"])
    check("收尾轮的产物进了最终回答（前端不会再显示空白）",
          WRAP in text, "%d 字" % len(text))
    check("有明确的「轮次用满」提示（用户知道为什么停）",
          any(("上限" in n and "轮" in n) for n in notes),
          [n[:42] for n in notes][:2])
    check("提示里给了可操作的下一步（回一句「继续」）",
          any("继续" in n for n in notes))


def main() -> int:
    print("数据目录：%s    MAX_TOOL_ROUNDS=%d    _CODE_CONTINUE_MAX=%d"
          % (TMP, M.MAX_TOOL_ROUNDS, T._CODE_CONTINUE_MAX))
    test_code_continue()
    test_rounds_exhausted()
    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  [!!] %s" % f)
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
