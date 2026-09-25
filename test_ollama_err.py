# -*- coding: utf-8 -*-
"""回归测试：Ollama 的 Go/JSON 报错要能识别成"工具调用写坏了"并自动重试。

背景（2026-09-25 用户报「❌ invalid character '\n' in string literal」）：
    Ollama 日志里的真身是
        source=qwen3vl.go:90 msg="qwen tool call parsing failed"
          error="invalid character '\n' in string literal"
    ——模型把工具调用参数里的换行写成了**裸换行**（没转义），Ollama 是 Go 写的，
    用 Go 的 json 解析器去解，于是吐出这句英文。
    应用原本的 _TOOLPARSE_MARKS 只按**后缀**枚举了三种写法，
    这一族（前缀都是 `invalid character`）漏了两个变体，用户那次正好踩在漏网的上面：
      · 没有自动重试（本来重发一次就好）
      · 用户看到的是英文原文（前端还会跟一句"可能内存/模型未就绪"，指错方向）

跑法：python test_ollama_err.py
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

TMP = tempfile.mkdtemp(prefix="mm_err_")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
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


# ⚠️ 下面这几条是 **Ollama 日志里抄出来的原始文本**，不要"美化"它们。
REAL_ERRORS = [
    "invalid character '\\n' in string literal",                    # ← 用户当天报的那条
    "invalid character '\u00e4' after object key:value pair",
    "invalid character '\u00e6' looking for beginning of value",
    "invalid character ''' looking for beginning of object key string",
    "unexpected end of JSON input",                                 # ← 已在旧名单里（对照）
]

print("=" * 62)
print("Ollama Go/JSON 报错的识别与自动重试")
print("=" * 62)

print("\n【一】这些真实报错都要被认出来（_is_toolparse_err）")
for e in REAL_ERRORS:
    check("识别: %s" % e[:52], M._is_toolparse_err(e))

print("\n【二】翻译成人话（_friendly_ollama_error）")
friendly = M._friendly_ollama_error(REAL_ERRORS[0])
check("是中文解释", "工具调用" in friendly and "英文原文" not in friendly)
check("说清「不是内存问题」", "不是内存" in friendly or "内存/显存" in friendly)
check("给出下一步（再发一次）", "再发一次" in friendly)
print("   实际文案：")
for line in friendly.strip().splitlines():
    print("     " + line)


class FakeResp:
    def __init__(self, chunks):
        self._lines = [json.dumps(c, ensure_ascii=False).encode("utf-8")
                       for c in chunks]

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def close(self):
        pass


async def run_once():
    """第 1 次：Ollama 吐工具调用解析错误；第 2 次：正常给出正文。"""
    state = {"n": 0}

    def fake_chat(messages, model=None, stream=True,
                  images_base64=None, params=None, tools=None):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResp([
                {"error": "invalid character '\\n' in string literal"},
            ])
        return FakeResp([
            {"message": {"thinking": "想一下"}, "done": False},
            {"message": {"content": "这是重试后的正常回答。"}, "done": False},
            {"done": True, "done_reason": "stop", "eval_count": 20},
        ])

    M.client.chat = fake_chat
    req = ChatRequest(messages=[{"role": "user", "content": "帮我记一下这件事"}],
                      session_id="test-ollama-err")
    resp = await M.chat(req)
    events = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        for line in chunk.split("\n"):
            if line.strip():
                events.append(json.loads(line))
    return events, state


print("\n【三】端到端：报了这个错，应用要自动重试并最终拿到正文")
events, state = asyncio.run(run_once())
notes = [e["note"] for e in events if e.get("note")]
done = next((e for e in events if e.get("done")), {})
check("自动重试了（第 1 次失败 → 第 2 次成功）", state["n"] == 2,
      "调用次数=%d" % state["n"])
check("给用户的中途提示是中文且不吓人",
      any("自动重试" in n for n in notes), notes[:2])
check("没有把英文原文直接丢给用户",
      not any("invalid character" in (e.get("error") or "") for e in events),
      "报错事件=%s" % [e.get("error") for e in events if e.get("error")][:1])
check("最终拿到了正文", (done.get("text") or "").strip() == "这是重试后的正常回答。",
      "正文=%r" % done.get("text"))

print("\n" + "=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 62)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
