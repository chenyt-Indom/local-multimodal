# -*- coding: utf-8 -*-
"""验证「Ollama 把模型写坏的工具调用解析失败」时，我们不再把英文报错丢给用户。

用户报的现象（2026-09-22）：
    ❌ invalid character '\\'' looking for beginning of object key string
       （可能内存/模型未就绪，请查看状态）
用户问"这个是什么原因" —— 说明这条提示**既没解释清楚、也指错了方向**。

真身（Ollama 服务端日志 `%LOCALAPPDATA%\\Ollama\\server.log`）：
    level=WARN source=qwen3vl.go:90 msg="qwen tool call parsing failed"
      error="invalid character '\\'' looking for beginning of object key string"
⇒ 是**模型自己**把工具调用的参数写成了单引号（或写残），Ollama 的 Go 解析器报错，
  它把这个 error 塞进流里，我们以前**原样转发**，前端再补一句"可能内存/模型未就绪"。
  跟内存/显存**毫无关系**，而且重新发一次基本就好。

这个脚本守四件事：
  A. 这类错误**自动重试**（第一版）：重试成功后用户看到的是正常回答，不是报错；
  B. 重试用完仍失败 → 给**中文解释 + 下一步怎么办**，不再出现裸英文当正文；
  C. 这类错误才会重试 —— **别的错误（模型不存在/连不上）必须立刻报**，不能无脑重试；
  D. 后端标记了 `explained` 的错误，前端不再补"（可能内存/模型未就绪…）"那句尾巴。

跑法（要 uvicorn/ollama 客户端那个解释器；不占 8000）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_ollama_error.py
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_ollamaerr_")
PORT_APP, PORT_STUB = 8771, 11603

# Ollama 真实吐出来的那句（照抄 server.log）
RAW_ERR = "invalid character '\\'' looking for beginning of object key string"
OK_REPLY = "好的，这是重试之后正常给出的回答。"

PASS, FAIL = 0, []
STUB_ROUNDS = []          # 每个用例收到过几次 /api/chat 请求


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {extra}")


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _ndjson(self, lines):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for obj in lines:
            s = json.dumps(obj) + "\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(s.encode()), s.encode()))
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json({"models": [{"name": "qwen3-vl:8b", "model": "qwen3-vl:8b",
                                    "size": 6 * 1024 ** 3}]})
        elif self.path.startswith("/api/version"):
            self._json({"version": "0.0.0-stub"})
        else:
            self._json({"models": []})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        txt = json.dumps(req.get("messages") or [], ensure_ascii=False)
        key = next((k for k in ("瞬时坏一次", "一直坏", "模型不存在", "普通提问")
                    if k in txt), "")
        STUB_ROUNDS.append(key)
        n_th = STUB_ROUNDS.count(key)

        if not req.get("stream", True):
            return self._json({"message": {"role": "assistant", "content": "好的。"},
                               "done": True, "done_reason": "stop"})

        if key == "瞬时坏一次" and n_th == 1:
            # 第一轮：先正常吐一点正文，再抛"工具调用解析失败"（和真机一样的顺序）
            return self._ndjson([
                {"message": {"role": "assistant", "content": "我来看一下。"}, "done": False},
                {"error": RAW_ERR, "done": True},
            ])
        if key == "一直坏":
            return self._ndjson([{"error": RAW_ERR, "done": True}])
        if key == "模型不存在":
            return self._ndjson([{"error": 'model "foo:1b" not found, try pulling it first',
                                  "done": True}])
        return self._ndjson([
            {"message": {"role": "assistant", "content": OK_REPLY}, "done": False},
            {"message": {"role": "assistant", "content": ""},
             "done": True, "done_reason": "stop"},
        ])


def free(p):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def chat(prompt, timeout=120):
    """返回 (事件列表, 正文, 错误事件列表, 备注列表)"""
    body = json.dumps({"messages": [{"role": "user", "content": prompt}],
                       "stream": True, "session_id": "oe-" + prompt[:6]},
                      ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request("http://127.0.0.1:%d/api/chat" % PORT_APP, data=body,
                               headers={"Content-Type": "application/json"})
    evs, text, errs, notes = [], [], [], []
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        for raw in resp:
            s = raw.decode("utf-8", "ignore").strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except Exception:
                continue
            evs.append(ev)
            if "error" in ev:
                errs.append(ev)
            if ev.get("note"):
                notes.append(ev["note"])
            m = ev.get("message")
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                text.append(m["content"])
    return evs, "".join(text), errs, notes


def main():
    print("=" * 64)
    print("Ollama 工具调用解析失败 —— 端到端（存根强制复现）")
    print("=" * 64)

    # ---------- 1. 纯函数：分类与翻译 ----------
    print("\n=== 1. 分类：只认「工具调用解析失败」这一类 ===")
    import backend.main as M
    check("真机那句（单引号）被判为工具解析错误", M._is_toolparse_err(RAW_ERR))
    check("unexpected end of JSON input 也算（同一类）",
          M._is_toolparse_err("unexpected end of JSON input"))
    check("qwen tool call parsing failed 也算", M._is_toolparse_err(
        "qwen tool call parsing failed"))
    # ⚠️ 负向：别的错误**不能**被当成这类，否则会被无脑重试、把真问题藏起来
    for other in ('model "foo:1b" not found, try pulling it first',
                  "connection refused", "context canceled", ""):
        check(f"（对照）{other[:34]!r} 不算", not M._is_toolparse_err(other))

    print("\n=== 2. 翻译：给中文解释 + 下一步，并明确「不是内存问题」 ===")
    t = M._friendly_ollama_error(RAW_ERR)
    check("翻成了中文", "工具调用格式不对" in t or "格式" in t)
    check("**明确否认**是内存/显存问题（原来那句误导就在这里）",
          "不是" in t and "内存" in t)
    check("给了下一步：再发一次", "再发一次" in t)
    check("保留了原始报错供排查", RAW_ERR.split()[0] in t)
    check("模型不存在 → 提示去换模型",
          "模型" in M._friendly_ollama_error('model "x" not found, try pulling it first'))
    check("连不上 → 提示启动 Ollama",
          "Ollama" in M._friendly_ollama_error("connection refused"))

    if not free(PORT_APP) or not free(PORT_STUB):
        check("端口空闲", False, "被占了，换端口再跑")
        return 1

    stub = ThreadingHTTPServer(("127.0.0.1", PORT_STUB), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    cfg["ollama_url"] = "http://127.0.0.1:%d" % PORT_STUB
    json.dump(cfg, open(os.path.join(TMP, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    srv_py = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs",
                          "Python", "Python314", "python.exe")
    if not os.path.isfile(srv_py):
        srv_py = sys.executable
    env = dict(os.environ, MM_DATA_DIR=TMP, PYTHONIOENCODING="utf-8")
    log = open(os.path.join(TMP, "srv.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [srv_py, "-c", "import uvicorn; from backend.main import app; "
         "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='warning')" % PORT_APP],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    try:
        base = "http://127.0.0.1:%d" % PORT_APP
        for _ in range(30):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=3).read()
                break
            except Exception:
                time.sleep(1)
        print("\n=== 3. 瞬时坏一次 → 应当自动重试后正常作答 ===")
        _, text, errs, notes = chat("【瞬时坏一次】帮我看看这个")
        check("没有把错误抛给用户", not errs, f"实得 {[e.get('error','')[:40] for e in errs]}")
        check("给了「正在重试」的提示（用户知道它在自救）",
              any("重试" in n for n in notes), f"notes={notes}")
        check("重试后拿到了正常回答", OK_REPLY in text, text[:80])
        check("确实重发了一次请求（stub 收到 2 次）",
              STUB_ROUNDS.count("瞬时坏一次") == 2, f"{STUB_ROUNDS.count('瞬时坏一次')} 次")

        print("\n=== 4. 一直坏 → 中文解释 + explained 标记，绝不能是裸英文 ===")
        STUB_ROUNDS.clear()
        _, text, errs, notes = chat("【一直坏】帮我看看这个")
        check("最后一定报错（不能无限重试）", bool(errs))
        e = errs[0] if errs else {}
        msg = str(e.get("error", ""))
        check("错误事件带 explained 标记（前端据此不再补尾巴）",
              e.get("explained") is True)
        check("是中文说明，不是裸英文", "工具调用格式不对" in msg, msg[:70].replace("\n", " "))
        check("明确说不是内存/显存问题", "不是" in msg and "内存" in msg)
        check("重试有上限（不会无限循环）",
              STUB_ROUNDS.count("一直坏") <= 3, f"{STUB_ROUNDS.count('一直坏')} 次")

        print("\n=== 5. 别的错误（模型不存在）→ 立刻报，不许重试 ===")
        STUB_ROUNDS.clear()
        _, _, errs, _ = chat("【模型不存在】帮我看看这个")
        check("报错了", bool(errs))
        check("**只请求了一次**（没把真问题当噪声重试）",
              STUB_ROUNDS.count("模型不存在") == 1,
              f"{STUB_ROUNDS.count('模型不存在')} 次")
        check("也给了中文解释", "模型" in str(errs[0].get("error", "")) if errs else False)

        print("\n=== 6. 普通提问不受影响 ===")
        STUB_ROUNDS.clear()
        _, text, errs, _ = chat("【普通提问】你好")
        check("正常作答、无报错", OK_REPLY in text and not errs, text[:60])

        print("\n=== 7. 前端：explained 的错误不再补「可能内存/模型未就绪」 ===")
        js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
        check("前端读到了 explained 标记", "obj.explained" in js)
        check("只有没被解释过的错误才补那句通用提示",
              'err.explained ? "" : "（可能内存/模型未就绪，请查看状态）"' in js)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        stub.shutdown()
        log.close()

    print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ! " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
