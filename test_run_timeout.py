# -*- coding: utf-8 -*-
"""验证「执行超过 25 秒，已被强制中止」这个问题真的修好了。

背景（用户真实反馈）：
  用户让模型写了个番茄钟 → 模型用工具跑它验证 → 25 秒硬上限到了被强杀 →
  工具原话是"执行超过 25 秒，已被强制中止" → 模型**以为自己的代码写错了**，
  于是加线程、加 signal.SIGALRM 反复重写（signal.alarm 在 Windows 上根本不存在），
  三版代码越改越烂 —— 其实用户的原版代码一行都没错，番茄钟本来就该跑 2 小时。

要验的六件事：
  ① 能认出「要一直跑」的程序（用**真实的** tomato_clock.py），且不误伤普通计算；
  ② 这种程序被跑时，结论里**明说不是报错**，而且给出已经产出的输出（回归：以前全丢）；
  ③ 一次性计算的上限提高了，并且可配置；
  ④ 真死循环仍然会被拦住（兜底不能破坏）；
  ⑤ 模型看到的文本里，"别改代码、交给用户点 ▶ 运行"这段话在**最前面**；
  ⑥ 用户手点「▶ 运行」这条路（/api/code/run_stream）不设时限、边跑边出字、能停。

跑法（⚠️ 用**带依赖的那个** Python314，不是 C:\Python314 ——
后者缺 python-multipart，起服务时会报 "Form data requires python-multipart"）：
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" test_run_timeout.py
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 临时数据目录 + 拷 config（⚠️ 不拷 config 会走 DEFAULT_CONFIG，得到假结论；
# 而且绝不能用真实数据目录 —— 那会污染用户看得见的东西）
TMP = tempfile.mkdtemp(prefix="mm_runtmo_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP

from backend import tools as T          # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


TOMATO = os.path.join(ROOT, "data", "workspace", "1", "tomato_clock.py")


def main():
    print("=" * 64)
    print("执行超时修复验证（临时数据目录：%s）" % TMP)
    print("=" * 64)

    # ---------------- ① 识别 ----------------
    print("\n① 认出「要一直跑」的程序，且不误伤普通计算")
    code_tomato = ""
    if os.path.isfile(TOMATO):
        with open(TOMATO, "r", encoding="utf-8") as f:
            code_tomato = f.read()
    check("拿到真实的番茄钟源码", "time.sleep(1)" in code_tomato,
          "路径 %s" % TOMATO)
    r = T.looks_persistent(code_tomato)
    check("番茄钟被认成持续运行型", bool(r), "原因=%r" % r)

    cases = [
        ("import time\nwhile True:\n    print(1)\n    time.sleep(1)", True, "无限循环+sleep"),
        ("import turtle\nturtle.forward(100)\nturtle.done()", True, "海龟窗口"),
        ("from http.server import HTTPServer\nHTTPServer(('',80), None).serve_forever()",
         True, "常驻服务器"),
        ("print(sum(range(100)))", False, "普通计算"),
        ("for i in range(3):\n    time.sleep(0.1)\nprint('ok')", False, "有次数的等待"),
        ("import time\nt=time.time()\nwhile time.time()-t < 3: pass\nprint('done')",
         False, "纯计算型（没有 sleep）"),
        ("import re\nprint(re.findall(r'\\d+', 'a1b2'))", False, "正则"),
    ]
    for code, want, label in cases:
        got = bool(T.looks_persistent(code))
        check("识别 · %s → %s" % (label, "持续运行" if want else "一次性"),
              got == want, "got=%s want=%s" % (got, want))

    # ---------------- ③ 上限 ----------------
    print("\n③ 一次性计算的上限")
    check("默认上限已提高（不再是 25 秒）", T.RUN_TIMEOUT >= 60, "RUN_TIMEOUT=%s" % T.RUN_TIMEOUT)
    check("run_timeout() 可读 config", T.run_timeout() == 60, "得到 %s" % T.run_timeout())
    check("冒烟窗口比上限短", 0 < T.RUN_PROBE < T.RUN_TIMEOUT, "RUN_PROBE=%s" % T.RUN_PROBE)

    # ---------------- ② 真跑番茄钟（走模型工具那条路）----------------
    print("\n② 跑真实番茄钟：结论必须说「不是报错」，且要带回已产出的输出")
    t0 = time.time()
    res = T.run_file(TOMATO)
    dt = round(time.time() - t0, 1)
    check("没有等满 60 秒（走的是冒烟窗口）", dt <= T.RUN_PROBE + 8, "用了 %s 秒" % dt)
    check("被标记为持续运行型", bool(res.get("persistent")), res.get("persistent"))
    check("带回了已产出的输出（回归：以前超时输出全丢）",
          "开始工作" in (res.get("out") or "") or "25:0" in (res.get("out") or ""),
          "out=%r" % (res.get("out") or "")[:80])
    check("措辞里明说「不是报错」", "不是报错" in (res.get("err") or ""),
          (res.get("err") or "")[:120])
    check("不再出现「已被强制中止」", "已被强制中止" not in (res.get("err") or ""))

    text = T._format_py_result(res)
    first_block = text.split("\n\n")[1] if len(text.split("\n\n")) > 1 else ""
    check("提醒放在最前面（弱模型才看得见）",
          "报错" in first_block and "▶ 运行" in first_block, "第一段=%r" % first_block[:80])
    check("明确禁止模型去改代码/绕超时",
          "不要" in text and ("signal" in text or "线程" in text))
    check("给了正确出路：让用户点 ▶ 运行", "▶ 运行" in text)

    # ---------------- ②b run_code 那条路 ----------------
    print("\n②b run_python 那条路：同样保留输出、同样说清楚")
    code = "import time\ni = 0\nwhile True:\n    print('tick', i, flush=True)\n    i += 1\n    time.sleep(1)\n"
    res2 = T.run_code(code, allow_risky=True)
    check("run_code 也认出来了", bool(res2.get("persistent")), res2.get("persistent"))
    check("run_code 超时也保住了输出（以前是空字符串）",
          "tick" in (res2.get("out") or ""), "out=%r" % (res2.get("out") or "")[:80])
    check("run_code 也说「不是报错」", "不是报错" in (res2.get("err") or ""))

    # ---------------- ④ 真死循环仍要被拦 ----------------
    print("\n④ 真死循环：兜底不能被破坏（但要说清「没有任何输出」这个疑点）")
    res3 = T.run_code("while True:\n    pass\n", allow_risky=True)
    check("死循环被拦下了（rc=None）", res3.get("rc") is None, "rc=%s" % res3.get("rc"))
    t3 = T._format_py_result(res3)
    check("没有输出时如实提示可能是卡住了", "死循环" in t3 or "没有输出" in t3,
          t3[:160].replace("\n", " "))

    # ---------------- ⑤ 普通代码不受影响 ----------------
    print("\n⑤ 普通代码照旧")
    res4 = T.run_code("print(1 + 1)")
    check("正常跑通", res4.get("rc") == 0 and (res4.get("out") or "").strip() == "2",
          "rc=%s out=%r" % (res4.get("rc"), res4.get("out")))
    check("没被误标成持续运行", not res4.get("persistent"))

    # ---------------- ⑥ 用户手点「▶ 运行」那条路 ----------------
    print("\n⑥ /api/code/run_stream：不设时限、边跑边出字、能停")
    _test_card_run()

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 64)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


def _test_card_run():
    """起一个**独立端口的临时实例**，真打 HTTP 接口。

    ⚠️ 两个坑，都踩过：
      · 绝不占 8000（会触发启动器的阻塞式 alert）→ 用 8765 + 临时数据目录；
      · 起服务的解释器必须是**带依赖的那个**（uvicorn + python-multipart）——
        `C:\\Python314\\python.exe` 缺 python-multipart，一 import 就 RuntimeError
        （报错信息是 "Form data requires python-multipart"，看着像 FastAPI 的问题，
         其实是解释器选错了）。
    """
    port = 8765
    srv_py = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                          "Programs", "Python", "Python314", "python.exe")
    if not os.path.isfile(srv_py):
        srv_py = sys.executable
    env = dict(os.environ)
    env["MM_DATA_DIR"] = TMP
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(TMP, "srv.log"), "w", encoding="utf-8")
    tmp_root = tempfile.gettempdir()
    before = {n for n in os.listdir(tmp_root) if n.startswith("mm_card_")}
    proc = subprocess.Popen(
        [srv_py, "-c",
         "import uvicorn; from backend.main import app; "
         "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='warning')" % port],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        base = "http://127.0.0.1:%d" % port
        up = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(base + "/api/ws/run_status", timeout=1).read()
                up = True
                break
            except Exception:
                time.sleep(0.5)
        check("临时实例起来了（端口 %d，不是 8000）" % port, up)
        if not up:
            try:
                with open(os.path.join(TMP, "srv.log"), encoding="utf-8") as f:
                    print(f.read()[-1200:])
            except Exception:
                pass
            return

        # 跑一个"本来就要一直跑"的程序：6 秒后仍然活着 → 证明没有时限
        code = ("import time\n"
                "i = 0\n"
                "while True:\n"
                "    i += 1\n"
                "    print('跑第 %d 秒' % i, end='\\r', flush=True)\n"
                "    time.sleep(1)\n")
        rid = "card-test"
        req = urllib.request.Request(
            base + "/api/code/run_stream",
            data=json.dumps({"code": code, "id": rid}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)

        # 关键：**边跑边出字** —— 而且这是 `end='\r'` 的进度行，
        # 以前用 readline() 会一直憋到进程结束（一个字都看不到）。
        chunks, saw_end = 0, False
        t0 = time.time()
        while time.time() - t0 < 6:
            line = resp.readline()
            if not line:
                break
            o = json.loads(line.decode("utf-8"))
            if o.get("t") == "out":
                chunks += 1
            elif o.get("t") == "end":
                saw_end = True
                break
        check("前 6 秒就收到了输出（不再是跑完才一次性吐）", chunks >= 2, "收到 %d 块" % chunks)
        check("6 秒后仍在运行、没被砍（不设时限）", not saw_end)
        check("run 登记表里有它（能停）",
              json.loads(urllib.request.urlopen(base + "/api/ws/run_status",
                                                timeout=5).read())["running"] >= 1)

        # 用户点「■ 停止」
        req = urllib.request.Request(
            base + "/api/ws/run_stop",
            data=json.dumps({"id": rid}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        stopped = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        check("停止按钮生效", bool(stopped.get("ok")), str(stopped))
        time.sleep(1.5)
        after = json.loads(urllib.request.urlopen(base + "/api/ws/run_status",
                                                  timeout=5).read())
        check("停止后没有残留进程", not after.get("running"), str(after))
        left = {n for n in os.listdir(tmp_root) if n.startswith("mm_card_")} - before
        check("临时工作目录被清掉了（不在 %TEMP% 里留垃圾）", not left, "残留：%s" % sorted(left))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            log.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
