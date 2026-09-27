# -*- coding: utf-8 -*-
"""验证「温度 temperature / top_p 能不能改、改了到底生不生效、会不会引发重载」。

用户问题（2026-09-27）：
  · "采样温度的修改以及 top_p 能不能修改并正常影响模型？"
  · "推荐值是不是还是和原来一样？"

分三段验证（前两段是**真的调模型**，不是读代码猜）：

  ① 管路：设置到底有没有进请求体？
     拦 `ollama_client.requests.request`，把发出去的 payload 抓下来看
     `options.temperature / options.top_p` —— 这一层错的话，后面都不用谈。

  ② 生效：同一个提问、不同采样参数，输出到底变不变？
     温度 0（贪心、应几乎可复现）vs 温度 1.9（应明显发散）；
     top_p=0.02（候选词极少、应接近贪心）vs top_p=0.95。

  ③ 副作用：改这两个参数**会不会引发模型重载**？
     看每次返回里的 `load_duration` —— 已加载时应当是 0~几毫秒；
     一旦几百毫秒~几十秒就说明被卸载重装了（那才是真问题，见 config.py 里
     num_ctx 必须一致的说明）。

跑法（必须 Python 3.14）：
    python scripts/verify_sampling.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_sampling_")
BASE_CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
json.dump(BASE_CFG, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import config as C          # noqa: E402
from backend import ollama_client as OC  # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name + (("   " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  [!!] " + name + (("   → " + str(detail)) if detail else ""))


# =====================================================================
#  ① 管路：拦 requests.request，看发出去的 payload
# =====================================================================
class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, chunks):
        self._lines = [json.dumps(c, ensure_ascii=False).encode("utf-8")
                       for c in chunks]

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def json(self):
        return {}


def test_plumbing():
    print("=" * 70)
    print("① 管路：设置有没有真的进请求体")
    print("=" * 70)

    seen = []
    import backend.ollama_client as oc_mod
    real_request = oc_mod.requests.request

    def fake_request(method, url, **kw):
        seen.append({"url": url, "json": kw.get("json"), "stream": kw.get("stream")})
        return _FakeResp([{"message": {"content": "ok"}, "done": False},
                          {"done": True, "done_reason": "stop"}])

    oc_mod.requests.request = fake_request
    try:
        cfg = C.load_config()
        cfg.update({"temperature": 0.11, "top_p": 0.22})
        cli = OC.OllamaClient()   # 地址由它自己从配置里读
        cli.chat([{"role": "user", "content": "hi"}], model="m", stream=True,
                 params=cfg)
    finally:
        oc_mod.requests.request = real_request

    p = (seen[-1]["json"] or {}) if seen else {}
    opt = p.get("options") or {}
    print("  实际发出去的 options = %s" % json.dumps(opt, ensure_ascii=False))
    check("temperature 进了请求体且值一致", opt.get("temperature") == 0.11, opt.get("temperature"))
    check("top_p 进了请求体且值一致", opt.get("top_p") == 0.22, opt.get("top_p"))
    check("num_ctx 也在（三者一起发）", opt.get("num_ctx") is not None, opt.get("num_ctx"))
    check("keep_alive 也在", p.get("keep_alive") is not None, p.get("keep_alive"))

    # 静态核对：聊天路径必须把 cfg 原样当 params 传下去（不能另组一份）
    src = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
    check("聊天路径用 `gen_params = dict(cfg)`（跟配置同源）",
          "gen_params = dict(cfg)" in src)
    check("聊天请求把 gen_params 交给 client.chat",
          "params=gen_params" in src)

    # 界面滑条：确认前端确实把两个值写回配置
    app_js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
    check("前端有 temperature 的写回", "temperature" in app_js)
    check("前端有 top_p 的写回", "top_p" in app_js)
    m = re.search(r'"/api/config"', app_js)
    check("前端通过 /api/config 落盘（改完立刻生效、不用重启）", bool(m))


# =====================================================================
#  ② / ③ 真的调模型：输出会不会变、会不会重载
# =====================================================================
def ask(base, model, temp=None, top_p=None, num_ctx=28672, npredict=64, prompt=None):
    opts = {"num_ctx": num_ctx, "num_predict": npredict}
    if temp is not None:
        opts["temperature"] = temp
    if top_p is not None:
        opts["top_p"] = top_p
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "keep_alive": "4h", "options": opts}
    req = urllib.request.Request(base + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    m = d.get("message") or {}
    # ⚠️⚠️ 必须把**思考**也当成模型的输出。
    #    qwen3-vl-think 是思考型模型：给 24 个 token 的额度时，它会**全部花在思考上**，
    #    `message.content` 是空的 —— 只比较 content 会得出"温度改了没影响"的假结论
    #    （实测踩过）。思考内容同样是采样出来的文本，照样受温度/top_p 影响。
    think = (m.get("thinking") or "").strip()
    body_text = (m.get("content") or "").strip()
    return {
        "text": (think + "␟" + body_text) if think else body_text,
        "think_only": bool(think) and not body_text,
        "load": d.get("load_duration") or 0,
        "wall": time.time() - t0,
        "evals": d.get("eval_count") or 0,
        "reason": d.get("done_reason") or "",
    }


def test_effect(model, base="http://127.0.0.1:11434"):
    print()
    print("=" * 70)
    print("② 生效：不同采样参数下，同一提问的输出会不会变")
    print("=" * 70)
    # 用"续写"型提问：答案空间大，最能看出采样差异
    PROMPT = "随便给我三个好听的中文女孩名字，只回名字，用顿号隔开。"

    def runs(**kw):
        out = []
        for _ in range(3):
            r = ask(base, model, prompt=PROMPT, **kw)
            out.append(r)
        return out

    print("  —— 温度 0.0（贪心，应当几乎可复现）——")
    cold = runs(temp=0.0)
    for r in cold:
        print("     %.2fs load=%.0fms tokens=%d(%s)  %s"
              % (r["wall"], r["load"] / 1e6, r["evals"], r["reason"] or "-",
                 r["text"][:48]))

    print("  —— 温度 1.9（应当明显发散）——")
    hot = runs(temp=1.9)
    for r in hot:
        print("     %.2fs load=%.0fms tokens=%d(%s)  %s"
              % (r["wall"], r["load"] / 1e6, r["evals"], r["reason"] or "-",
                 r["text"][:48]))

    c_set = {r["text"] for r in cold}
    h_set = {r["text"] for r in hot}
    check("温度 0 时输出基本可复现（3 次里只有 1 种）", len(c_set) == 1,
          "不同输出 %d 种" % len(c_set))
    check("温度 1.9 时输出发散了（多于 1 种）", len(h_set) > 1,
          "不同输出 %d 种" % len(h_set))

    print("  —— top_p（温度固定 1.0，改候选范围）——")
    narrow = [ask(base, model, temp=1.0, top_p=0.02, prompt=PROMPT) for _ in range(2)]
    wide = [ask(base, model, temp=1.0, top_p=0.95, prompt=PROMPT) for _ in range(2)]
    for tag, rs in (("top_p=0.02", narrow), ("top_p=0.95", wide)):
        print("     %s:" % tag)
        for r in rs:
            print("        %.2fs tokens=%d  %s" % (r["wall"], r["evals"], r["text"][:48]))
    check("top_p=0.02 时输出趋于稳定（候选词被压到极少）",
          len({r["text"] for r in narrow}) == 1,
          "不同输出 %d 种" % len({r["text"] for r in narrow}))

    print()
    print("=" * 70)
    print("③ 副作用：改采样参数会不会引发模型重载")
    print("=" * 70)
    allr = cold + hot + narrow + wide
    worst = max(r["load"] for r in allr)
    print("  全部 %d 次调用里，最大的 load_duration = %.0f ms" % (len(allr), worst / 1e6))
    for r in allr[1:]:
        if r["load"] > 1e9:
            print("     ⚠️ 有一次 load=%.1fs（=重载了）" % (r["load"] / 1e9))
    check("改温度/top_p **不**引发重载（load_duration 全部 < 1 秒）",
          worst < 1e9, "最大 %.0f ms" % (worst / 1e6))
    check("每次调用都能拿到输出（思考也算）", all(r["text"] for r in allr),
          "空输出 %d 次" % sum(1 for r in allr if not r["text"]))
    if all(r["think_only"] for r in allr):
        print("  说明：这 %d 次都只吐了思考、没吐正文（思考型模型 + 短额度），"
              "所以上面的比较比的是**思考文本** —— 它同样受采样参数影响。" % len(allr))


def test_recommended():
    print()
    print("=" * 70)
    print("④ 推荐值（当前代码里的默认值 + 注释里的依据）")
    print("=" * 70)
    d = C.DEFAULT_CONFIG
    print("   temperature = %s" % d.get("temperature"))
    print("   top_p       = %s" % d.get("top_p"))
    print("   repeat_penalty = %s" % d.get("repeat_penalty"))
    print("   repeat_last_n  = %s" % d.get("repeat_last_n"))
    check("temperature 仍是 0.6（2026-09-19 从 0.7 调下来的）",
          float(d.get("temperature")) == 0.6, d.get("temperature"))
    check("top_p 仍是 Qwen3 官方推荐的 0.95",
          float(d.get("top_p")) == 0.95, d.get("top_p"))
    check("repeat_penalty 仍是 1.2（>1.3 会结巴）",
          float(d.get("repeat_penalty")) == 1.2, d.get("repeat_penalty"))
    check("repeat_last_n 仍是 1024（Ollama 默认 64 正是「打转」的元凶）",
          int(d.get("repeat_last_n")) == 1024, d.get("repeat_last_n"))
    # 运行中的配置（用户可能自己拖过滑条）也要报出来
    live = C.load_config()
    print("   你现在生效的值：temperature=%s top_p=%s"
          % (live.get("temperature"), live.get("top_p")))


def main() -> int:
    test_plumbing()
    test_recommended()
    # 只对"已安装"的默认模型做真实调用
    model = str(BASE_CFG.get("default_model") or "qwen3-vl-think:30b")
    print()
    print("（真实调用模型：%s）" % model)
    test_effect(model)
    print()
    print("=" * 70)
    print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  [!!] %s" % f)
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
