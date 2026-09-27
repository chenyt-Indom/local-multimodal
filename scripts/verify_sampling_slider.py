# -*- coding: utf-8 -*-
"""端到端验证「拖采样滑条 → 立刻生效」：起一个临时实例，给请求打探针抓真实 payload。

⚠️ 为什么**不比较两次输出**（第一版就是那么写错的）：
   应用会把「当前时间：HH:MM:SS」拼到最后一条用户消息末尾（那是为了前缀缓存能命中）。
   于是**每一轮的提示词都不一样** → 即使 temperature=0，两次输出也可能不同。
   拿"输出是否一致"当判据会误判成"设置没生效"。
⇒ 正确判据：**拖完滑条后，应用下一次请求里带的就是新值**（这一层对了，
   模型侧的效果由 scripts/verify_sampling.py 用真实调用证明：温度 0 可复现、
   1.9 发散、top_p 0.02 收窄）。

跑法（需要 Python 3.14）：python scripts/verify_sampling_slider.py [端口]
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
B = "http://127.0.0.1:%d" % PORT

TMP = tempfile.mkdtemp(prefix="mm_slider2_")
SPY = os.path.join(TMP, "spy_options.jsonl")
cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
cfg.update({"web_enabled": False, "rag_enabled": False, "memory_enabled": False,
            "auto_memorize": False, "voice_auto_start": False,
            "code_auto_route": False})
json.dump(cfg, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
          ensure_ascii=False)

# 启动临时实例；启动前把 requests.request 换成"记录 /api/chat 的 options"的探针
BOOT = r'''
import json, os, sys
sys.path.insert(0, r"%s")
import backend.ollama_client as oc
_spy_path = r"%s"
_real = oc.requests.request
def _spy(method, url, **kw):
    try:
        if "/api/chat" in str(url) and isinstance(kw.get("json"), dict):
            body = kw["json"]
            rec = {"options": body.get("options"), "keep_alive": body.get("keep_alive")}
            with open(_spy_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return _real(method, url, **kw)
oc.requests.request = _spy
import uvicorn
from backend.main import app
uvicorn.run(app, host="127.0.0.1", port=%d, log_level="warning")
''' % (ROOT, SPY, PORT)

env = dict(os.environ, MM_DATA_DIR=TMP, PYTHONPATH=ROOT)
server = subprocess.Popen([sys.executable, "-c", BOOT], cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name + (("   " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  [!!] " + name + (("   → " + str(detail)) if detail else ""))


def wait_up(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(B + "/api/config", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def set_cfg(**kw):
    req = urllib.request.Request(B + "/api/config", data=json.dumps(kw).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["config"]


def chat_once(tag):
    body = {"messages": [{"role": "user", "content": "只回两个字：收到"}],
            "stream": True, "session_id": "spy-" + tag}
    req = urllib.request.Request(B + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if json.loads(line).get("done"):
                break


def last_spy():
    if not os.path.exists(SPY):
        return None
    lines = [l for l in open(SPY, encoding="utf-8").read().splitlines() if l.strip()]
    return json.loads(lines[-1]) if lines else None


def main():
    if not wait_up():
        print("临时实例起不来（端口 %d）" % PORT)
        return 2
    print("临时实例已就绪：%s" % B)
    orig = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))

    # ① 拖到温度 0.15 / top_p 0.35 → 下一轮请求必须带这两个值
    set_cfg(temperature=0.15, top_p=0.35)
    chat_once("a")
    got = last_spy() or {}
    opt = got.get("options") or {}
    print("  第 1 次抓到：%s" % json.dumps(opt, ensure_ascii=False))
    check("改了 temperature 后，下一次请求就带上了新值",
          opt.get("temperature") == 0.15, opt.get("temperature"))
    check("改了 top_p 后，下一次请求就带上了新值",
          opt.get("top_p") == 0.35, opt.get("top_p"))

    # ② 再拖一组，确认不是"只有第一次生效"
    set_cfg(temperature=1.7, top_p=0.8)
    chat_once("b")
    opt2 = (last_spy() or {}).get("options") or {}
    print("  第 2 次抓到：%s" % json.dumps(opt2, ensure_ascii=False))
    check("再改一次同样立刻生效（temperature=1.7）",
          opt2.get("temperature") == 1.7, opt2.get("temperature"))
    check("top_p 也跟着变（0.8）", opt2.get("top_p") == 0.8, opt2.get("top_p"))

    # ③ 必须与"模型加载"用同一套 options（否则会引发卸载重装，见 warm_options）
    check("请求里同时带着 num_ctx（与预热同源，不会触发重载）",
          opt2.get("num_ctx") is not None, opt2.get("num_ctx"))
    check("keep_alive 也在（保活 4h）", (last_spy() or {}).get("keep_alive") is not None,
          (last_spy() or {}).get("keep_alive"))

    print()
    print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  [!!] %s" % f)
    return 0 if not FAIL else 1


try:
    code = main()
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()
    shutil.rmtree(TMP, ignore_errors=True)
    print("临时实例已停止、数据目录已清理")
sys.exit(code)
