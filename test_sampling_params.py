# -*- coding: utf-8 -*-
"""守住「温度 / top_p 能改、改完立刻生效」这条链路（2026-09-27）。

用户问题："采样温度的修改以及 top_p 能不能修改并正常影响模型？"
          "推荐值是不是还是和原来一样？"

链路共三段，**任何一段断了用户都会觉得"滑条没用"**：
  ① 前端滑条 → POST /api/config 落盘
  ② 聊天请求把配置原样当 options 发出去（`gen_params = dict(cfg)` → `params=gen_params`）
  ③ 预热请求必须带**同一套** num_ctx/temperature，否则 Ollama 会判定"配置变了"
     卸掉重装（实测每 20 秒一次，见 ollama_client.warm_options）

本文件只做**不调模型**的快速断言（真实效果由 scripts/verify_sampling.py 用
真实调用证明：温度 0 可复现、1.9 发散、top_p 0.02 收窄，且 load_duration ~0）。

跑法：python test_sampling_params.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="mm_samplingtest_")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
json.dump(CFG, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
os.environ["MM_DATA_DIR"] = TMP

from backend import config as C            # noqa: E402
from backend import ollama_client as OC    # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name + (("   " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  [!!] " + name + (("   → " + str(detail)) if detail else ""))


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


def main() -> int:
    print("=" * 66)
    print("① 请求体：配置里的采样参数有没有原样发出去")
    print("=" * 66)
    seen = []
    real = OC.requests.request

    def fake_request(method, url, **kw):
        seen.append({"json": kw.get("json")})
        return _FakeResp([{"message": {"content": "ok"}, "done": False},
                          {"done": True, "done_reason": "stop"}])

    OC.requests.request = fake_request
    try:
        cfg = C.load_config()
        cfg.update({"temperature": 0.33, "top_p": 0.44})
        OC.OllamaClient().chat([{"role": "user", "content": "hi"}],
                               model="m", stream=True, params=cfg)
    finally:
        OC.requests.request = real

    payload = (seen[-1]["json"] or {}) if seen else {}
    opt = payload.get("options") or {}
    check("temperature 原样进请求体", opt.get("temperature") == 0.33,
          opt.get("temperature"))
    check("top_p 原样进请求体", opt.get("top_p") == 0.44, opt.get("top_p"))
    check("num_ctx 同时发送（三者一组）", opt.get("num_ctx") is not None,
          opt.get("num_ctx"))
    check("keep_alive 也在（保活，避免隔几分钟就被卸载）",
          payload.get("keep_alive") is not None, payload.get("keep_alive"))

    print()
    print("=" * 66)
    print("② 聊天路径：必须与配置同源（不能另组一份参数）")
    print("=" * 66)
    main_src = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
    check("聊天用 `gen_params = dict(cfg)`", "gen_params = dict(cfg)" in main_src)
    check("把 gen_params 交给 client.chat", "params=gen_params" in main_src)

    print()
    print("=" * 66)
    print("③ 预热请求：必须带同一套 num_ctx（否则每次预热都会引发重载）")
    print("=" * 66)
    wo = OC.warm_options()
    live = C.load_config()
    check("预热 num_ctx 与聊天一致",
          int(wo.get("num_ctx")) == int(live.get("num_ctx") or 8192),
          "预热 %s / 配置 %s" % (wo.get("num_ctx"), live.get("num_ctx")))
    check("预热也带上 temperature（同源）", "temperature" in wo, wo.get("temperature"))
    check("预热是 1 个 token 的空推理", int(wo.get("num_predict") or 0) == 1,
          wo.get("num_predict"))
    t2i_src = open(os.path.join(ROOT, "backend", "t2i.py"), encoding="utf-8").read()
    check("画图后的预热也走 warm_options（不是自己拼一份）",
          "warm_options" in t2i_src)

    print()
    print("=" * 66)
    print("④ 推荐值没被改动")
    print("=" * 66)
    d = C.DEFAULT_CONFIG
    check("temperature = 0.6", float(d.get("temperature")) == 0.6, d.get("temperature"))
    check("top_p = 0.95（Qwen3 官方推荐）", float(d.get("top_p")) == 0.95,
          d.get("top_p"))
    check("repeat_penalty = 1.2（>1.3 会结巴）",
          float(d.get("repeat_penalty")) == 1.2, d.get("repeat_penalty"))
    check("repeat_last_n = 1024（Ollama 默认 64 会打转）",
          int(d.get("repeat_last_n")) == 1024, d.get("repeat_last_n"))

    print()
    print("=" * 66)
    print("⑤ 前端滑条：改完要落盘（不用重启）")
    print("=" * 66)
    js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
    check("前端有 temperature 滑条", "temperature" in js)
    check("前端有 top_p 滑条", "top_p" in js)
    check("前端把改动 POST 给 /api/config", '"/api/config"' in js)
    if '"temperature"' not in js:
        print("  （提示：滑条可能换了变量名，请人工确认一次）")

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  [!!] %s" % f)
    print("=" * 66)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
