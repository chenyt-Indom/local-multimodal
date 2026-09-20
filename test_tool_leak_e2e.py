# -*- coding: utf-8 -*-
"""端到端验证「模型把工具调用泄漏进正文」：

起一个**存根 Ollama**，让它故意吐一段包在 `<function-call>` / `<tool_call>`
里的调用（而且**拆成两个 chunk** 推，顺便验流式扣留），再对着临时实例发一次请求，
检查用户最终看到的东西。

### 为什么必须这么测
用户报的是"偶尔出现"，实机跑了 5 次一次都没复现（模型仍走原生通道）——
**偶发问题不能靠"多试几次"来验**。存根能把那一瞬间**必然**造出来。

### 判据（缺一不可）
  ① 用户可见的增量里**从头到尾**没有 `<function-…` / 裸 JSON —— 界面上不会闪出来；
  ② 那个调用**真的被执行了**（有 tool_start 事件）—— 不只是"藏起来了事"；
  ③ 它周围的正常文字**保留**（没连坐删掉）；
  ④ 第二轮（工具结果喂回后）的回答正常合并。

跑法（带依赖的那个 Python314）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_tool_leak_e2e.py
"""
import io
import json
import os
import shutil
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
TMP = tempfile.mkdtemp(prefix="mm_leak_e2e_")
PORT_APP, PORT_STUB = 8767, 11599

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------
# 存根 Ollama：按"这一轮是不是第一轮"决定吐什么
# --------------------------------------------------------------------------
LEAK_A = ("我帮你在知识库里查一下。\n\n"
          "<function-call>\n"
          "{\n"
          '  "name": "search_knowledge",\n'
          '  "arguments": {\n'
          '    "query": "名侦探柯南 中柯哀 vs 新兰 角色塑造 社会价值观 分析",\n'
          '    "list_all": false\n'
          "  }\n"
          "}\n"
          "</function-call>\n")
LEAK_B = ('好的，我看下时间。\n<tool_call>\n'
          '{"name": "get_time", "arguments": {}}\n</tool_call>\n')
ANSWER2 = "根据你知识库里的资料，这样的对比可以分三个层面来看。"

STUB_CALLS = []          # 记录 stub 收到的请求，便于排错


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send_json({"models": [{"name": "qwen3-vl:8b", "model": "qwen3-vl:8b",
                                         "size": 6 * 1024 ** 3}]})
        elif self.path.startswith("/api/version"):
            self._send_json({"version": "0.0.0-stub"})
        elif self.path.startswith("/api/ps"):
            self._send_json({"models": []})
        else:
            self._send_json({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        msgs = req.get("messages") or []
        txt = json.dumps(msgs, ensure_ascii=False)
        STUB_CALLS.append({"path": self.path, "stream": bool(req.get("stream"))})
        # 第二轮：已经喂回工具结果了 → 给正常回答（让循环收尾）
        round2 = '"tool"' in txt or "工具结果：" in txt
        if round2:
            content = ANSWER2
        elif "【泄漏测试A】" in txt:
            content = LEAK_A
        elif "【泄漏测试B】" in txt:
            content = LEAK_B
        else:
            content = "好的。"          # 记忆提炼等后台调用

        if req.get("stream", True):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 拆成两块推：第二块的起点正好落在 "<function-call" 的中间，
            # 专门验"流式时把可能是壳子的尾巴先扣住"。
            if content is LEAK_A:
                pieces = ["我帮你在知识库里查一下。\n\n<fun",
                          'ction-call>\n{\n  "name": "search_knowledge",\n'
                          '  "arguments": {\n    "query": "名侦探柯南 中柯哀 vs 新兰 '
                          '角色塑造 社会价值观 分析",\n    "list_all": false\n  }\n}\n'
                          "</function-call>\n"]
            else:
                pieces = [content]
            for p in pieces:
                line = json.dumps({"message": {"role": "assistant", "content": p},
                                   "done": False}) + "\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line.encode()), line.encode()))
            done = json.dumps({"message": {"role": "assistant", "content": ""},
                               "done": True, "done_reason": "stop"}) + "\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(done.encode()), done.encode()))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._send_json({"message": {"role": "assistant", "content": content},
                             "done": True, "done_reason": "stop"})


def free_port(p):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def main():
    print("=" * 64)
    print("工具调用泄漏 —— 端到端（存根 Ollama 强制复现）")
    print("=" * 64)

    if not free_port(PORT_APP) or not free_port(PORT_STUB):
        check("端口空闲（%d / %d）" % (PORT_APP, PORT_STUB), False, "被占了，换一个再跑")
        return 1

    # 存根 Ollama
    stub = ThreadingHTTPServer(("127.0.0.1", PORT_STUB), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    print("  存根 Ollama: http://127.0.0.1:%d" % PORT_STUB)

    # 临时数据目录 + 指向存根的配置
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["ollama_url"] = "http://127.0.0.1:%d" % PORT_STUB
    with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    srv_py = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                          "Programs", "Python", "Python314", "python.exe")
    if not os.path.isfile(srv_py):
        srv_py = sys.executable
    env = dict(os.environ)
    env["MM_DATA_DIR"] = TMP
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(TMP, "srv.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [srv_py, "-c",
         "import uvicorn; from backend.main import app; "
         "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='warning')" % PORT_APP],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT_APP

    def shutdown():
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        stub.shutdown()

    try:
        up = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1).read()
                up = True
                break
            except Exception:
                time.sleep(0.5)
        check("临时实例起来了（端口 %d）" % PORT_APP, up)
        if not up:
            print(io.open(os.path.join(TMP, "srv.log"), encoding="utf-8").read()[-1500:])
            return 1

        for tag, q, tool_name in (
            ("A · <function-call>", "【泄漏测试A】名侦探柯南中柯哀和新兰哪对更符合现代价值观", "search_knowledge"),
            ("B · <tool_call>", "【泄漏测试B】现在几点", "get_time"),
        ):
            print("\n场景 %s" % tag)
            deltas, events, notes = [], [], []
            body = json.dumps({"messages": [{"role": "user", "content": q}],
                               "stream": True}).encode("utf-8")
            req = urllib.request.Request(base + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                for raw in r:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        o = json.loads(raw)
                    except Exception:
                        continue
                    m = o.get("message") or {}
                    if m.get("content"):
                        deltas.append(m["content"])
                    if o.get("tool_start"):
                        events.append(o["tool_start"].get("name"))
                    if o.get("note"):
                        notes.append(o["note"])
            visible = "".join(deltas)

            # ① 用户从头到尾看不到壳子
            bad = [k for k in ("<function", "<tool_call", "<tool-call", '"arguments"', '"list_all"')
                   if k in visible]
            check("① 用户可见的流里没有泄漏块", not bad, "命中 %s" % bad if bad else repr(visible[:50]))
            # ② 调用真的执行了
            check("② 调用真的被执行（不是藏起来就完事）", tool_name in events, str(events))
            # ③ 周围的正常文字保留
            keep = "我帮你在知识库里查一下" if tool_name == "search_knowledge" else "我看下时间"
            check("③ 它周围的正常文字保留", keep in visible, repr(visible[:60]))
            # ④ 第二轮回答合并进来
            check("④ 工具结果喂回后的回答也在了", ANSWER2 in visible, repr(visible[-60:]))
            print("     可见正文 %r" % visible[:90])

    finally:
        shutdown()
        time.sleep(0.3)

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 64)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
