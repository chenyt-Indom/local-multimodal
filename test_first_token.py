# -*- coding: utf-8 -*-
"""守住「发出去半天没反应」这条修复。

背景（2026-09-22 实测，Qwen3-VL:8B / RTX 5070 Ti）：
  用户反馈"我发 prompt 之后，要等很久 AI 才开始流式输出思考过程"。
  查下来是：系统提示的**第一行**是「当前时间：…HH:MM:SS」（带秒），
  每发一条消息提示词的第一个 token 就变了 → Ollama 的前缀缓存（KV 复用）
  永远命不中 → 每轮都要把整份提示词重新预填充。
  Ollama 日志：`prompt eval time = 10607 ms / 19837 tokens` —— 10.6 秒全在重算。

修法：把时间挪到**最后一条用户消息的末尾**（排在系统提示+工具定义之后），
      并给聊天请求加 keep_alive（默认只保活 5 分钟，超时卸载模型 → 冷加载），
      启动时主动预热默认模型（原来预热只挂在已移除的"开发台"上，从未执行）。

跑法：python test_first_token.py   （必须用 Python 3.14）
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_firsttok_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import config as C     # noqa: E402
from backend import main as M       # noqa: E402
from backend import ollama_client as OC  # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
SRC_OC = open(os.path.join(ROOT, "backend", "ollama_client.py"), encoding="utf-8").read()
CFG = C.load_config()

print("=" * 62)
print("① 时间戳不再放在系统提示里（缓存杀手）")
sysp = M._SystemPrompt.build("你好", "demo", None)
check("系统提示第一句不是「当前时间」", not sysp.lstrip().startswith("当前时间"),
      sysp[:40])
check("⚠️ 系统提示里**根本没有**带秒的时间（否则前缀每次都变）",
      "当前时间：" + M._now_str()[:6] not in sysp and "：" + M._now_str()[-8:] not in sysp)
check("⚠️ 系统提示里**没有**任何 HH:MM:SS 形态的时间（缓存杀手）",
      not re.search(r"\d{1,2}:\d{2}:\d{2}", sysp))
# 结构检查：_now_str 只在 _attach_live_ctx 里被调用（别处再加回去就会破坏缓存）
uses = [l.strip() for l in SRC_MAIN.splitlines() if "_now_str()" in l]
check("_now_str() 只在 _attach_live_ctx 内被调用（别处不许再加）",
      all(("+ _now_str() +" in l) or ("def _now_str" in l) for l in uses),
      f"实得 {uses}")

print()
print("=" * 62)
print("② 时间确实还能被模型看到（挪到用户消息末尾）")
msgs = [{"role": "system", "content": sysp},
        {"role": "user", "content": "今天几号？"}]
out = M._attach_live_ctx(msgs)
check("最后一条用户消息末尾带上了「当前时间」", "当前时间" in out[-1]["content"])
check("时间在用户消息里，且在原问题之后",
      out[-1]["content"].startswith("今天几号？"), out[-1]["content"][:40])
check("⚠️ 原消息**没有被就地改动**（不能把时间戳写进落盘历史）",
      msgs[-1]["content"] == "今天几号？")
check("返回的是新列表", out is not msgs)

print()
print("=" * 62)
print("③ 两次调用：稳定前缀应当**逐字节相同**（缓存能命中的前提）")
a = M._attach_live_ctx([{"role": "system", "content": M._SystemPrompt.build("你好")},
                   {"role": "user", "content": "第一句"}])
b = M._attach_live_ctx([{"role": "system", "content": M._SystemPrompt.build("你好")},
                   {"role": "user", "content": "第二句"}])
check("系统提示部分逐字节一致（前缀可复用）", a[0]["content"] == b[0]["content"])
check("只有用户那一轮不同（本来就该不同）", a[-1]["content"] != b[-1]["content"])
sys_first = a[0]["content"][:200]
check("系统提示开头 200 字里不含秒级时间",
      not re.search(r"\d{1,2}:\d{2}:\d{2}", sys_first), sys_first[:60])

print()
print("=" * 62)
print("④ 记忆 / 知识库 / 历史摘要都不许待在系统提示里（它们每轮都会变）")
# 只算"真代码行"：注释里会提到这个名字（用来解释为什么挪走），不该被当成调用
_code_lines = [l for l in SRC_MAIN.splitlines() if not l.lstrip().startswith("#")]
_calls = [l.strip() for l in _code_lines if "kb.build_rag_context" in l]
check("全项目只剩一处 kb.build_rag_context 调用（在聊天接口里挂给用户消息）",
      len(_calls) == 1, f"实得 {len(_calls)} 处：{_calls}")
check("这一处挂在 rag_ctx 上（不是塞回系统提示）",
      any("rag_ctx = kb.build_rag_context" in l for l in _calls), str(_calls))
check("RAG 由 _attach_live_ctx 挂载（有 rag_ctx 参数）", "rag_ctx: str" in SRC_MAIN)
check("记忆由 _memory_ctx() 统一组装", "def _memory_ctx" in SRC_MAIN)
check("记忆块挂在用户消息上（mem_ctx 参数）", "mem_ctx: str" in SRC_MAIN)
check("历史摘要挂在用户消息上（digest 参数）", "digest: str" in SRC_MAIN)
check("⚠️ full_sys 不再拼接 digest（否则裁剪一变就废掉整段前缀）",
      "full_sys = sys_prompt" in SRC_MAIN)
_mem_calls = [l.strip() for l in _code_lines if "_memory_ctx(" in l]
check("_memory_ctx 只在函数定义与聊天接口里出现", len(_mem_calls) == 2, str(_mem_calls))

_rag = M._attach_live_ctx([{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "问题"}],
                          mem_ctx="【记忆】他叫陈工",
                          rag_ctx="【知识库资料】\n[资料1](a.pdf)\n正文",
                          digest="用户：开头聊了 A；助手：回了 B")
check("有资料时：资料出现在用户消息里，原问题还在",
      "资料1" in _rag[-1]["content"] and "问题" in _rag[-1]["content"])
check("记忆与摘要也都进了用户消息",
      "陈工" in _rag[-1]["content"] and "开头聊了 A" in _rag[-1]["content"])
check("⚠️ 三段材料都排在问题之前（先背景后问题，读起来才顺）",
      _rag[-1]["content"].index("资料1") < _rag[-1]["content"].index("【用户这一轮说的话】"))
_norag = M._attach_live_ctx([{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "问题"}])
check("没资料时：用户消息不被塞进空资料块", "【知识库资料】" not in _norag[-1]["content"])
check("带资料时也不会污染原消息（历史要干净）",
      "资料1" not in _rag[0]["content"])

print()
print("=" * 62)
print("⑤ 模型保活（避免 5 分钟不用就被卸载、下次冷加载）")
check("配置里有 model_keep_alive", "model_keep_alive" in CFG, str(CFG.get("model_keep_alive")))
check("默认值是 30m（不再是 Ollama 的 5 分钟默认）",
      str(CFG.get("model_keep_alive") or "").endswith("m"), CFG.get("model_keep_alive"))
check("ollama_client.chat 会把 keep_alive 带进请求体",
      'payload["keep_alive"]' in SRC_OC)


class _FakeResp:
    status_code = 200

    def json(self):
        return {}


captured = {}


def _fake_req(self, method, path, **kw):
    captured.update(kw.get("json") or {})
    return _FakeResp()


_real = OC.OllamaClient._req
OC.OllamaClient._req = _fake_req
try:
    OC.OllamaClient().chat([{"role": "user", "content": "hi"}], model="m", stream=False,
                           params={"max_tokens": 10, "temperature": 0.6})
finally:
    OC.OllamaClient._req = _real
check("实际请求体里确实有 keep_alive", "keep_alive" in captured, str(list(captured)))
check("keep_alive 的值来自配置", captured.get("keep_alive") == CFG.get("model_keep_alive"),
      str(captured.get("keep_alive")))

print()
print("=" * 62)
print("⑥ 启动预热（原来挂在已移除的「开发台」上，从未执行）")
check("有启动预热钩子", "_warmup_on_start" in SRC_MAIN)
check("预热只针对默认模型（显存装不下两个）",
      "default_model" in SRC_MAIN.split("def _warmup_on_start")[1].split("@app.post")[0])
check("配置里有开关 warmup_on_start", "warmup_on_start" in CFG)
check("预热可以关掉（warmup_on_start=False 时不预热）",
      'cfg.get("warmup_on_start"' in SRC_MAIN)
check("ws_warm 复用了同一个实现（不是两份代码）", "def _warm_models" in SRC_MAIN)
check("预热失败不影响使用（只记日志）", "预热模型 %s 失败" in SRC_MAIN)

print()
print("=" * 62)
print("⑦ 工具定义规模（说明 2 万 token 提示词的大头在哪）")
from backend import tools as T  # noqa: E402
sch = T.make_schemas(True, True, True)
tok = M._est_tokens(json.dumps(sch, ensure_ascii=False))
print("     工具定义：%d 个，约 %d token" % (len(sch), tok))
check("工具定义规模被记录下来了（超过 8000 token 时值得警惕）", tok > 0)
check("系统提示本身不超过 5000 token", M._est_tokens(sysp) < 5000,
      M._est_tokens(sysp))

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
