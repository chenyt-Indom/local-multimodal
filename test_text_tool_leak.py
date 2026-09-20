# -*- coding: utf-8 -*-
"""验证「工具调用泄漏成代码卡片」+「input() 没法输入」这两个问题都修好了。

### 背景（用户报的两个问题，同一轮对话里都出现了）
用户让模型"生成一个计时代码"，结果界面上：
  ① 冒出好几张**一模一样的代码卡片**，其中一张的"代码"其实是
     `{"name": "workspace_write", "arguments": {...}}` 这坨 JSON ——
     点它的「▶ 运行」得到 `SyntaxError: '{' was never closed`；
  ② 程序里有 `input("请输入计时的秒数: ")`，跑起来却**没法输入**，
     模型只能看到 EOF / ValueError，于是它把本来没错的代码改来改去。

### 根因
① 代码模型那一轮是**文本协议**：工具调用是写在正文里的 ```tool 块。
   `_split_text_tool_calls` 以前是"JSON 解不开就 continue"→ 那个块**原样留在正文里**。
   而这次模型吐的 JSON **漏了最外层的 `}`**（实测原文见下面的 REAL_BROKEN），
   json.loads 失败 → 块没删 → 前端把每个围栏块都渲染成一张「可运行」的代码卡片。
   模型还爱吐一对**空的** ```tool 当分隔符，同样没被删 → 又多几张空卡片。
② 模型工具跑代码时 stdin 没人喂（EOF）；卡片「▶ 运行」虽然接了管道，
   但界面上**没有输入框**（原来那个输入框是跟「开发台」一起删掉的）。

### 本测试验什么
  ① 显式工具围栏（```tool / ```tool_call）**一律从正文里删掉**，空的也删；
  ② 漏了 `}` 的 JSON 能被**补回来**并真的执行（不再静默丢掉一次调用）；
  ③ 真的截断（字符串都没闭合）时**不硬补**，但正文里也不会看到那坨 JSON；
  ④ 正常的 ```json / 正文里的 JSON 数据**不能被误删**；
  ⑤ `run_code` / `run_python` 支持 stdin；
  ⑥ 用**真实那一轮的原文**回放一遍（文件在的话）。

跑法（用带依赖的那个 Python314）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_text_tool_leak.py
"""
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="mm_toolleak_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP

from backend import main as M           # noqa: E402
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


# 真实那一轮里模型吐出来的东西（照原文抄的，就少了最外层那个 `}`）
REAL_BROKEN = (
    "好的，以下是代码：\n\n```python\nimport time\nprint('hi')\n```\n\n"
    "将这个代码保存到一个文件中，例如 `timer.py`，然后运行它：\n\n"
    "```tool\n"
    '{"name": "workspace_write", "arguments": {"rel": "timer.py", '
    '"text": "import time\\ndef timer(duration):\\n    while duration:\\n'
    "        print(duration)\\n        time.sleep(1)\\n        duration -= 1\\n"
    '    print(\\"计时结束\\")\\n\\nif __name__ == \\"__main__\\":\\n'
    '    duration = int(input(\\"请输入计时的秒数: \\"))\\n    timer(duration)"}\n'
    "```\n\n"
    "然后运行这个文件：\n\n```tool\n\n```\n\n"
    "运行结果将会在终端中显示倒计时。\n"
)


def main():
    print("=" * 64)
    print("工具调用泄漏 / input() 输入 —— 修复验证")
    print("=" * 64)

    # ---------------- ① 显式工具围栏一律删掉 ----------------
    print("\n① ```tool 围栏（含空的）一律从正文里删掉")
    calls, cleaned = M._split_text_tool_calls(REAL_BROKEN)
    check("正文里不再有 ```tool 残留", "```tool" not in cleaned and "```tool" not in cleaned)
    check("正文里不再有 \"name\" 那坨 JSON", '"arguments"' not in cleaned
          and "workspace_write" not in cleaned,
          cleaned[-60:].replace("\n", " "))
    check("正常的 ```python 代码块**保留**（那是给用户看的）",
          "```python" in cleaned and "print('hi')" in cleaned)
    check("普通文字保留", "运行结果将会在终端中显示倒计时" in cleaned)

    # ---------------- ② 残缺 JSON 要补救并执行 ----------------
    print("\n② 漏了最外层 `}` 的工具调用：补回来并真的执行")
    check("解析出了 workspace_write", any(c["name"] == "workspace_write" for c in calls),
          [(c["name"], list(c["arguments"].keys())) for c in calls])
    got = next((c for c in calls if c["name"] == "workspace_write"), None)
    if got:
        a = got["arguments"]
        check("rel 正确", a.get("rel") == "timer.py", a.get("rel"))
        code = a.get("text") or ""
        check("text 拿到完整代码", "def timer(duration)" in code
              and code.rstrip().endswith("timer(duration)"),
              repr(code[-30:]))
        check("换行是真的换行（不是字面量 \\n）", "\n" in code and "\\n" not in code)

    # ---------------- ③ 真空截断：不硬补，但也不许露出来 ----------------
    print("\n③ 截断在半截字符串里的 JSON：不硬补（否则会把半截代码当完整的写盘）")
    check("_loads_lenient 对'字符串没闭合'返回 None",
          M._loads_lenient('{"name": "workspace_write", "arguments": {"text": "print(1)') is None)
    trunc = "看这个：\n\n```tool\n{\"name\": \"workspace_write\", \"arguments\": {\"rel\": \"a.py\", \"text\": \"print(1)\n```\n"
    _c2, clean2 = M._split_text_tool_calls(trunc)
    check("块仍然被删掉了（不给用户看那坨 JSON）", "```tool" not in clean2
          and "workspace_write" not in clean2, repr(clean2[:60]))
    check("没有把它当成有效调用执行", _c2 == [], str(_c2))

    # ---------------- ④ 别误删正常 JSON ----------------
    print("\n④ 正常 JSON 数据不能被误删")
    data = ("这是接口返回：\n\n```json\n{\"name\": \"张三\", \"age\": 20}\n```\n\n"
            "还有一个：\n\n```json\n{\"arguments\": [1, 2, 3]}\n```\n")
    _c3, clean3 = M._split_text_tool_calls(data)
    check("```json 数据块保留", "```json" in clean3 and "张三" in clean3)
    check("没被当成工具调用", _c3 == [], str(_c3))

    # ---------------- ④b 右边那种（合法的 ```tool）照旧执行 ----------------
    print("\n④b 写得完整、合法的 ```tool：照样执行 + 不露在正文里")
    good = ("这就写：\n\n```tool\n"
            '{"name": "workspace_write", "arguments": {"rel": "b.py", "text": "print(2)"}}\n'
            "```\n\n好了。\n")
    c4, clean4 = M._split_text_tool_calls(good)
    check("解析出 1 个调用", len(c4) == 1 and c4[0]["name"] == "workspace_write", str(c4))
    check("正文里没有 JSON 残留", "```tool" not in clean4 and '"rel"' not in clean4, repr(clean4))

    # ---------------- ⑤ stdin ----------------
    print("\n⑤ 带 input() 的代码：喂了 stdin 就能正常跑")
    code_in = ("s = input('请输入秒数: ')\n"
               "print('收到', s)\n")
    r1 = T.run_code(code_in)                                   # 不喂
    check("不喂 stdin → 照旧报 EOF（不是静默挂住）",
          r1.get("rc") not in (0, None) or "EOF" in (r1.get("err") or ""),
          "rc=%s err=%s" % (r1.get("rc"), (r1.get("err") or "")[:60]))
    r2 = T.run_code(code_in, stdin_text="7")                    # 喂了
    check("喂了 stdin → 正常跑完", r2.get("rc") == 0
          and "收到 7" in (r2.get("out") or ""),
          "rc=%s out=%r err=%r" % (r2.get("rc"), (r2.get("out") or "")[:40],
                                   (r2.get("err") or "")[:60]))
    ev = []
    txt = T._do_run_python({"code": code_in, "stdin": "9"}, ev)
    check("run_python 工具认 stdin 参数", "收到 9" in txt, txt[:80].replace("\n", " "))

    print("\n⑤b 带 input() 却没喂 → 工具结果里要**说清不是代码错了**")
    txt2 = T._do_run_python({"code": code_in}, [])
    check("提示里点名 input()/stdin 并给出出路",
          "input()" in txt2 and "stdin" in txt2, txt2[-160:].replace("\n", " "))

    # ---------------- ④c 自动兜底存文库：别把工具调用存成源码 ----------------
    print("\n④c 自动兜底存文库：不许把工具调用存成源码文件")
    check("工具围栏块被认成内部调用",
          M._looks_like_tool_block(
              "tool", '{"name": "workspace_write", "arguments": {"rel": "a.py"}}'))
    check("普通代码块不会被误判",
          not M._looks_like_tool_block("python", "import time\nprint(1)\n"))
    check("```json 数据块不会被误判",
          not M._looks_like_tool_block("json", '{"name": "张三", "age": 20}'))
    M._autosave_answer(REAL_BROKEN, "生成一个计时代码")
    leaked = []
    for root, _dirs, files in os.walk(TMP):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if "workspace_write" in f.read():
                        leaked.append(fn)
            except Exception:
                pass
    check("写进文库的文件里没有那坨工具 JSON", not leaked, str(leaked))

    # ---------------- ⑥ 回放真实会话里残留的工具块 ----------------
    # ⚠️ 以前这里**写死了某个会话文件名**，那个文件一被新的对话改写（内容变了）就会
    #    假失败（2026-09-21 踩到）。改成扫**全部**会话；一条样本都没有就明确跳过 ——
    #    在其他机器上跑更是必然没有样本，不能因此报错。
    print("\n⑥ 回放真实会话里残留的工具块（有样本才检查）")
    samples, bad = 0, []
    for sp in sorted(glob.glob(os.path.join(ROOT, "sessions", "*.json"))):
        try:
            with open(sp, "r", encoding="utf-8") as f:
                sd = json.load(f)
        except Exception:
            continue
        s_msgs = sd if isinstance(sd, list) else (sd.get("messages") or [])
        for m in s_msgs:
            c = str(m.get("content") or "")
            if m.get("role") != "assistant" or "```tool" not in c:
                continue
            samples += 1
            _cc, cl = M._split_text_tool_calls(c)
            if "```tool" in cl or '"arguments"' in cl:
                bad.append(os.path.basename(sp))
    if samples:
        check("真实会话里残留的工具块都能清掉", not bad,
              "检查了 %d 条，文件 %s" % (samples, sorted(set(bad))))
    else:
        check("（跳过：历史会话里暂时没有工具块样本）", True)

    # ---------------- ⑦ 端到端：卡片运行 + 真·键盘输入 ----------------
    print("\n⑦ 端到端：程序等 input() 时，界面上能真的把内容送进去")
    _test_interactive_run()

    # ---------------- ⑧ 泄漏成 <function-call> / <tool_call> 的（默认模型）----------------
    # 2026-09-21 用户反馈：默认模型 qwen3-vl 偶尔把整个调用包一层 XML 写进**正文**，
    # 而 Ollama 的原生通道只认它自己模板里的 <tool_call> → tool_calls 为空 →
    # 以前只在代码模型那轮做的清理没跑到 → 那坨 JSON 原样渲染给用户看，工具压根没执行。
    print("\n⑧ 默认模型把调用包成 XML 写进正文（截图那一轮的原样文本）")
    LEAK_XML = (
        "我帮你在知识库里查一下。\n\n"
        "<function-call>\n"
        "{\n"
        '  "name": "search_knowledge",\n'
        '  "arguments": {\n'
        '    "query": "名侦探柯南 中柯哀 vs 新兰 角色塑造 社会价值观 分析",\n'
        '    "list_all": false\n'
        "  }\n"
        "}\n"
        "</function-call>\n"
    )
    _c, cl = M._split_text_tool_calls(LEAK_XML, allowed={"search_knowledge"}, bare=False)
    check("壳子里的调用被认出来（不再静默丢掉一次调用）",
          len(_c) == 1 and _c[0]["name"] == "search_knowledge", str(_c)[:120])
    check("参数完整", _c and _c[0]["arguments"].get("query", "").startswith("名侦探柯南"),
          str(_c[0]["arguments"])[:90] if _c else "")
    check("正文里不再有 <function-call> 壳子", "function-call" not in cl.lower(), repr(cl[:70]))
    check("正文里不再有裸 JSON", '"arguments"' not in cl and '"arguments"' not in cl,
          repr(cl[:70]))
    check("它自己的说明文字**保留**", "我帮你在知识库里查一下" in cl, repr(cl[:40]))

    print("\n⑧b 各种写法都要认得（大小写 / 空格 / 下划线 / 半截 / 空壳）")
    for name, txt in (
        ("<tool_call>", '前文\n<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>\n后文'),
        ("<FUNCTION-CALL> 大写", '前文\n<FUNCTION-CALL>\n{"name": "get_time", "arguments": {}}\n</FUNCTION-CALL>'),
        ("<function_call> 下划线", '前文\n<function_call>\n{"name": "get_time", "arguments": {}}\n</function_call>'),
        ("<function call> 带空格", '前文\n<function call>\n{"name": "get_time", "arguments": {}}\n</function call>'),
        ("只写了开标签（被截断）", '前文\n<function-call>\n{"name": "get_time", "arguments": {}}'),
        ("空壳（一对空标签）", "前文\n<function-call></function-call>\n后文"),
    ):
        _c2, cl2 = M._split_text_tool_calls(txt, allowed={"get_time"}, bare=False)
        want_call = "空壳" not in name
        ok = (len(_c2) == 1) if want_call else (len(_c2) == 0)
        check("%s → 调用%s" % (name, "认出来" if want_call else "不执行"),
              ok and "function" not in cl2.lower() and "tool_call" not in cl2.lower(),
              "calls=%s clean=%r" % (_c2, cl2[:60]))

    print("\n⑧c ⚠️ 别把正文里**正常的 JSON** 当成调用（聊天轮 bare=False）")
    normal = ('这是接口返回的示例：\n```json\n{"name": "张三", "arguments": {"age": 20}}\n```\n'
              "还有裸着的：{\"name\": \"library\", \"arguments\": {\"action\": \"list\"}}\n请参考。")
    _c3, cl3 = M._split_text_tool_calls(normal, allowed={"library"}, bare=False)
    check("裸 JSON **不**被当成调用", _c3 == [], str(_c3))
    check("正常内容一个字都没少", cl3 == normal.strip(), "长度 %d → %d" % (len(normal), len(cl3)))
    # 同一段文本在**代码轮**（bare=True）才该被认出来 —— 那轮模型本来就用文本协议
    _c3b, _ = M._split_text_tool_calls(normal, allowed={"library"}, bare=True)
    check("代码轮仍然认得裸 JSON（回归）", len(_c3b) == 1 and _c3b[0]["name"] == "library",
          str(_c3b))

    print("\n⑧d ⚠️ 只执行「本轮真给过它的工具」，别的只清文本")
    _c4, cl4 = M._split_text_tool_calls(LEAK_XML, allowed={"get_time"}, bare=False)
    check("没给它的工具**不执行**（防止绕开开关）", _c4 == [], str(_c4))
    check("但文本仍然清干净（用户看不到裸 JSON）",
          "function-call" not in cl4.lower() and '"arguments"' not in cl4, repr(cl4[:60]))

    print("\n⑧e 逐字流式的「扣留」标记集：聊天轮要**保守**，别毁掉流式体感")
    plain = "这是答案。接口字段是 {\"name\": \"x\", \"arguments\": {}}，就这样。"
    check("聊天轮：正文里的 JSON **不扣**（该发的照发）",
          M._safe_emit_len(plain, M._LEAK_MARKS) == len(plain),
          "扣到 %d / 共 %d" % (M._safe_emit_len(plain, M._LEAK_MARKS), len(plain)))
    check("聊天轮：<function-call> 起点就扣住",
          M._safe_emit_len("答案如下\n<function-call>\n{", M._LEAK_MARKS) == len("答案如下\n"),
          str(M._safe_emit_len("答案如下\n<function-call>\n{", M._LEAK_MARKS)))
    check("聊天轮：```tool 也扣住",
          M._safe_emit_len("答案```tool\n{}", M._LEAK_MARKS) == len("答案"))
    check("聊天轮：结尾半个 '<' 也先按住",
          M._safe_emit_len("答案<", M._LEAK_MARKS) == len("答案"),
          str(M._safe_emit_len("答案<", M._LEAK_MARKS)))
    check("代码轮（原标记集）行为没变：```json 也会扣",
          M._safe_emit_len(plain, M._TEXT_TOOL_MARKS) < len(plain),
          str(M._safe_emit_len(plain, M._TEXT_TOOL_MARKS)))

    print("\n⑧f 「半个标签」：模型写到一半放弃留下的碎片（实机复现过）")
    # 实机看到的现象：正文开头冒出一截 `<function`，后面直接接正常文字。
    # 它不是调用（没有 JSON），但留着就像乱码。
    for name, txt, keep in (
        ("单独占一行", "<function\n以下是键值对的示例", "以下是键值对的示例"),
        ("正文最开头", "<function以下是键值对的示例", "以下是键值对的示例"),
        ("<tool_call 半截独占一行", "<tool_call\n正文开始", "正文开始"),
        ("半截 + 前后都有正文", "前面的话\n<function\n后面的话", "前面的话"),
    ):
        _c5, cl5 = M._split_text_tool_calls(txt, allowed=set(), bare=False)
        check("%s → 碎片被清掉" % name,
              not re.search(r"<\s*/?\s*(?:function|tool)", cl5, re.I) and keep in cl5,
              repr(cl5[:60]))
    _c6, cl6 = M._split_text_tool_calls(
        "<function> 是 JS 里定义函数的关键字，写法是 function foo() {}",
        allowed=set(), bare=False)
    check("⚠️ 讲标签用法的句子不被整段吃掉",
          "是 JS 里定义函数的关键字" in cl6, repr(cl6[:70]))
    check("碎片不会被当成调用执行", _c6 == [] and _c5 == [], "%s / %s" % (_c6, _c5))

    print("\n" + "=" * 64)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 64)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


def _test_interactive_run():
    """起一个**独立端口的临时实例**，跑一个 input() 程序，再从"界面"喂一行进去。

    ⚠️ 端口用 8765、数据目录用临时的 —— 绝不占正在跑的那个 8000；
       起服务的解释器必须用**带依赖的那个**（缺 python-multipart 会 import 失败）。
    """
    import subprocess
    import time
    import urllib.request

    port = 8766
    srv_py = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                          "Programs", "Python", "Python314", "python.exe")
    if not os.path.isfile(srv_py):
        srv_py = sys.executable
    env = dict(os.environ)
    env["MM_DATA_DIR"] = TMP
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(TMP, "srv2.log"), "w", encoding="utf-8")
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
        check("临时实例起来了（端口 %d）" % port, up)
        if not up:
            return

        code = ("s = input('请输入秒数: ')\n"
                "print('好的，开始', s, '秒')\n")
        rid = "stdin-test"
        req = urllib.request.Request(
            base + "/api/code/run_stream",
            data=json.dumps({"code": code, "id": rid}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)

        # 程序没有换行、直接在等输入 —— 提示语必须**立刻**推出来（readline 会憋住）
        got_prompt = False
        t0 = time.time()
        while time.time() - t0 < 5:
            line = resp.readline()
            if not line:
                break
            o = json.loads(line.decode("utf-8"))
            if o.get("t") == "out" and "请输入秒数" in o.get("data", ""):
                got_prompt = True
                break
        check("等待输入时的提示语能实时显示（不需要换行）", got_prompt)

        # 模拟用户在卡片输入框里打字 → 回车
        req = urllib.request.Request(
            base + "/api/ws/run_input",
            data=json.dumps({"id": rid, "data": "5"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        sent = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        check("输入送进去了", bool(sent.get("ok")), str(sent))

        tail, rc = "", None
        t0 = time.time()
        while time.time() - t0 < 8:
            line = resp.readline()
            if not line:
                break
            o = json.loads(line.decode("utf-8"))
            if o.get("t") == "out":
                tail += o.get("data", "")
            elif o.get("t") == "end":
                rc = o.get("rc")
                break
        check("程序收到输入并正常结束", rc == 0, "rc=%s" % rc)
        check("输出里能看到它用上了我们输入的 5", "好的，开始 5 秒" in tail,
              repr(tail[-60:]))
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
