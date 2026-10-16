# -*- coding: utf-8 -*-
"""本地多模态助手 · 桌面窗口启动器

解决问题：
1. 原版双击后只启动后台服务、不弹界面，用户以为"闪退"
2. 服务已在运行时再次双击，端口 8000 被占用 -> 程序报错退出（真闪退）

本启动器逻辑：
- 先探测 http://127.0.0.1:8000 是否已就绪
    已就绪  -> 直接复用，绝不重复启动（杜绝端口冲突闪退）
    未就绪  -> 以隐藏控制台方式后台拉起 exe，轮询等待就绪
- 就绪后用 pywebview 打开原生桌面窗口（无地址栏/无标签页）
- 任何失败都用弹窗提示原因，而不是静默退出
- 窗口关闭时，仅结束"本次由启动器拉起"的服务进程

依赖：pywebview（已装在独立虚拟环境 envs/webview 中）
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

try:
    import winreg
except ImportError:      # 非 Windows 上不致命，默认目录退化成主目录
    winreg = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"
# 轻量健康检查端点：纯内存返回（实测 0.03s）。
# 根路径 / 要读 index.html（FileResponse），磁盘忙或冷启动时会明显变慢。
HEALTH_URL = URL + "/api/health"
TITLE = "本地多模态助手 · Qwen3-VL 8B"
WINDOW_W, WINDOW_H = 1280, 860

# ---------------------------------------------------------------------------
# 服务启动目标（2026-10-16 起支持「便携整合包」布局）：
#
#   <包根>\
#     启动窗口.pyw        ← 本文件（也可用 启动.bat 拉起）
#     app\run.py          ← 代码
#     runtime\python\     ← Python 运行时（含 fastapi/torch/diffusers 等依赖）
#     ollama\ollama.exe   ← 内置 Ollama（由本启动器拉起，不依赖系统托盘版）
#     models\{ollama, sd, rembg}
#     data\               ← 记忆 / 知识库 / 文库 / config.json
#
# **优先用包内的运行时与代码**，找不到才回退到"开发机布局"（源码 + 系统 Python）。
# 于是同一个启动器既能跑便携包，也能在这台开发机上继续跑源码。
# ---------------------------------------------------------------------------
_BUNDLE_DIR = APP_DIR
_BUNDLE_APP_DIR = os.path.join(_BUNDLE_DIR, "app")
_BUNDLE_RUNTIME_PYW = os.path.join(_BUNDLE_DIR, "runtime", "python", "pythonw.exe")
_BUNDLE_OLLAMA_EXE = os.path.join(_BUNDLE_DIR, "ollama", "ollama.exe")
_BUNDLE_MODELS = os.path.join(_BUNDLE_DIR, "models")
_BUNDLE_DATA = os.path.join(_BUNDLE_DIR, "data")

# ⚠️⚠️ 包内 Ollama 用**独立端口 11435**，不抢系统的 11434（2026-10-16 定的）。
# 为什么必须这样：系统装的 Ollama Windows 托盘版在"启动"文件夹里**开机自启**，
# 它会先占住 11434；而它**无视 OLLAMA_MODELS**、只认自己 db.sqlite 的模型目录。
# 于是会出现：整合包启动 → 看到 11434 有人在听 → 直接复用 → 而那个 Ollama
# 手里根本没有我们的模型 → 又变成"model not found"。
# 换个端口后，包内 Ollama 永远是权威的，机器上装没装 Ollama 都不影响它。
_BUNDLE_OLLAMA_PORT = 11435
_BUNDLE_OLLAMA_URL = "http://127.0.0.1:%d" % _BUNDLE_OLLAMA_PORT

_SRC_DIR = r"D:\local-multimodal-src"
_SRC_RUN = os.path.join(_SRC_DIR, "run.py")
# 注意：本机有两个 Python，**只有下面这个装了项目依赖**（fastapi/torch 等），
# 另一个 C:\Python314 缺 python-multipart，用它启动会直接报错。
_PYW_CANDIDATES = [
    _BUNDLE_RUNTIME_PYW,                       # ← 便携包自带，最可靠，排第一
    r"C:\Users\19853\AppData\Local\Programs\Python\Python314\pythonw.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                 "Python314", "pythonw.exe"),
    r"C:\Python314\pythonw.exe",
]


def _pick_pythonw():
    """找一个可用的 pythonw.exe（无控制台窗口）。"""
    for p in _PYW_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _resolve_service_cmd():
    """返回 (启动命令, 工作目录)：优先包内 app/，其次源码，最后 exe。"""
    pyw = _pick_pythonw()
    for d in (_BUNDLE_APP_DIR, _SRC_DIR):
        run_py = os.path.join(d, "run.py")
        if pyw and os.path.isfile(run_py):
            return [pyw, run_py], d
    return [os.path.join(APP_DIR, "本地多模态助手.exe")], APP_DIR


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """端口上有没有人在听（用来判断"服务/Ollama 是不是已经在跑"）。"""
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def ensure_bundle_env() -> bool:
    """便携包：把数据/模型目录**相对自身**锚定到包内。

    用 `setdefault` 之外的强制赋值是故意的 —— 包内布局就是唯一真相，
    这样整块拷到别的盘/别的机器（盘符变了）也照样对。
    同时把 `MM_OLLAMA_URL` 指向**包内 Ollama 的独立端口**（见 `_BUNDLE_OLLAMA_PORT`），
    这样就算机器上还跑着别的 Ollama，也不会被"复用"到空壳上。
    """
    if not os.path.isdir(_BUNDLE_MODELS):
        return False
    os.environ["MM_BUNDLE_ROOT"] = _BUNDLE_DIR
    os.environ["MM_MODELS_DIR"] = _BUNDLE_MODELS
    os.environ["MM_DATA_DIR"] = _BUNDLE_DATA
    if os.path.isfile(_BUNDLE_OLLAMA_EXE):
        os.environ["MM_OLLAMA_URL"] = _BUNDLE_OLLAMA_URL
    try:
        os.makedirs(_BUNDLE_DATA, exist_ok=True)
    except Exception:
        pass
    return True


def ensure_ollama(timeout: float = 90.0) -> str:
    """确保**包内的** Ollama 在跑（独立端口，不依赖系统托盘版）。

    ⚠️ 为什么必须自带（2026-10-16 的教训）：Windows 托盘版 `ollama app.exe`
    **无视 OLLAMA_MODELS 环境变量**，只认它自己 `%LOCALAPPDATA%\\Ollama\\db.sqlite`
    里的设置；一旦升级/重置，模型目录就被打回默认的 `~/.ollama/models`
    （那通常是个目录联接，而 Ollama 0.40 读不了 reparse point）——
    用户看到的就是"模型全没了 / 404 model not found"。
    自己拉起 + 把 OLLAMA_MODELS 指到包内 `models\\ollama`，这个变量就彻底消失了。

    ⚠️ 端口用 `_BUNDLE_OLLAMA_PORT`（11435）而不是 11434：系统的托盘版开机自启、
    会先占住 11434，而它手里没有我们的模型 —— 复用它就等于又坏一次。
    """
    if _port_open("127.0.0.1", _BUNDLE_OLLAMA_PORT):
        return "包内 Ollama 已在 %d 端口上运行" % _BUNDLE_OLLAMA_PORT
    if not os.path.isfile(_BUNDLE_OLLAMA_EXE):
        return "包内没有 ollama.exe，跳过（将使用系统安装的 Ollama）"
    env = dict(os.environ)
    env["OLLAMA_MODELS"] = os.path.join(_BUNDLE_MODELS, "ollama")
    env["OLLAMA_HOST"] = "127.0.0.1:%d" % _BUNDLE_OLLAMA_PORT
    env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [_BUNDLE_OLLAMA_EXE, "serve"],
            cwd=os.path.dirname(_BUNDLE_OLLAMA_EXE),
            env=env, creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return "⚠️ 包内 Ollama 启动失败：%s" % e
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                    _BUNDLE_OLLAMA_URL + "/api/version", timeout=3):
                return ("包内 Ollama 已启动（端口 %d，模型目录 = models\\ollama）"
                        % _BUNDLE_OLLAMA_PORT)
        except Exception:
            time.sleep(1.0)
    return "⚠️ 包内 Ollama 启动超时（请检查 ollama 目录与显卡驱动）"


SERVICE_CMD, SERVICE_CWD = _resolve_service_cmd()

# ---------------------------------------------------------------------------
# 页面增强脚本：给界面补上"拖拽图片"和"Ctrl+V 粘贴图片"能力
# 原前端只支持点击 🖼 按钮选文件，不含拖拽/粘贴监听。
# 注入方式：把拖入/粘贴得到的图片塞进 #fileInput.files，再派发 change 事件，
#          复用页面原有的 onchange 逻辑，无需改动打包好的 exe。
# ---------------------------------------------------------------------------
INJECT_JS = """
(function () {
  if (window.__mmDropPasteReady) { return "already"; }
  window.__mmDropPasteReady = true;

  var st = document.createElement("style");
  st.textContent = "#mmDropMask{position:fixed;left:0;top:0;right:0;bottom:0;" +
    "background:rgba(18,24,36,.78);color:#fff;font-size:20px;z-index:99999;" +
    "display:none;align-items:center;justify-content:center;" +
    "border:3px dashed #4c8dff;box-sizing:border-box;pointer-events:none;}" +
    "#mmDropMask.show{display:flex;}";
  document.head.appendChild(st);

  var mask = document.createElement("div");
  mask.id = "mmDropMask";
  mask.textContent = "松开鼠标 · 添加图片";
  document.body.appendChild(mask);

  function feed(files) {
    var input = document.getElementById("fileInput");
    if (!input || !files || !files.length) { return 0; }
    var dt = new DataTransfer();
    var n = 0;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (f && f.type && f.type.indexOf("image/") === 0) { dt.items.add(f); n++; }
    }
    if (!n) { return 0; }
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return n;
  }

  var depth = 0;
  document.addEventListener("dragenter", function (e) {
    e.preventDefault(); e.stopPropagation();
    depth++; if (mask) { mask.classList.add("show"); }
  }, true);
  document.addEventListener("dragover", function (e) {
    e.preventDefault(); e.stopPropagation();
    if (e.dataTransfer) { e.dataTransfer.dropEffect = "copy"; }
  }, true);
  document.addEventListener("dragleave", function (e) {
    e.preventDefault(); e.stopPropagation();
    depth--; if (depth <= 0) { depth = 0; mask.classList.remove("show"); }
  }, true);
  document.addEventListener("drop", function (e) {
    e.preventDefault(); e.stopPropagation();
    depth = 0; mask.classList.remove("show");
    if (e.dataTransfer && e.dataTransfer.files) { feed(e.dataTransfer.files); }
  }, true);

  document.addEventListener("paste", function (e) {
    var cd = e.clipboardData;
    if (!cd || !cd.items) { return; }
    var picked = [];
    for (var i = 0; i < cd.items.length; i++) {
      var it = cd.items[i];
      if (it.kind === "file" && it.type && it.type.indexOf("image/") === 0) {
        var f = it.getAsFile();
        if (f) { picked.push(f); }
      }
    }
    if (picked.length) { e.preventDefault(); feed(picked); }
  }, true);

  /* ---- 实时时间注入：每次请求自动把当前时间告诉模型 ---- */
  if (!window.__mmTimePatched) {
    window.__mmTimePatched = true;
    window.__mmOrigFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        var url = (typeof input === "string") ? input : ((input && input.url) || "");
        if (url.indexOf("/api/chat") !== -1 && init && typeof init.body === "string") {
          var data = JSON.parse(init.body);
          if (data && Object.prototype.toString.call(data.messages) === "[object Array]") {
            var d = new Date();
            var wk = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"][d.getDay()];
            var p2 = function (n) { return (n < 10 ? "0" : "") + n; };
            var ts = d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
                     p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds()) + " " + wk;
            data.messages = data.messages.filter(function (m) {
              return !(m && m.role === "system" && typeof m.content === "string" &&
                       m.content.indexOf("[实时时间]") === 0);
            });
            data.messages.unshift({
              role: "system",
              content: "[实时时间] 现在是 " + ts +
                       "（北京时间，来自本机系统时钟）。用户询问时间、日期、星期、年份时" +
                       "必须以此为准，不要使用训练数据中的时间。"
            });
            init = Object.assign({}, init, { body: JSON.stringify(data) });
          }
        }
      } catch (err) { }
      return window.__mmOrigFetch.call(this, input, init);
    };
  }

  return "ok";
})();
"""

# 自检脚本：确认拖拽/粘贴可用，并验证"请求中确实被插入了实时时间"
_SELFCHECK_JS = (
    "(function(){try{"
    "var out={};"
    "var dt=new DataTransfer();"
    "out.dataTransfer=!!(dt&&dt.items&&typeof dt.items.add==='function');"
    "out.fileInput=!!document.getElementById('fileInput');"
    "out.attachArea=!!document.getElementById('attachments');"
    "out.sendBtn=!!document.getElementById('sendBtn');"
    "out.timePatched=window.__mmTimePatched===true;"
    "var d=new Date();"
    "var p=function(n){return (n<10?'0':'')+n;};"
    "out.now=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());"
    "try{"
    "var cap=null;var real=window.__mmOrigFetch;"
    "window.__mmOrigFetch=function(u,i){cap=i;return Promise.resolve({});};"
    "window.fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({messages:[{role:'user',content:'x'}],stream:false})});"
    "window.__mmOrigFetch=real;"
    "out.timeInjected=!!(cap&&typeof cap.body==='string'&&cap.body.indexOf('实时时间')>=0);"
    "}catch(e2){out.timeInjected='ERR:'+e2.message;}"
    "return JSON.stringify(out);"
    "}catch(e){return 'ERR:'+e.message;}})()"
)


def alert(msg: str, title: str = "本地多模态助手") -> None:
    """Windows 弹窗提示（避免静默闪退，用户看不到原因）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        print(msg)


LOG_FILE = os.path.join(tempfile.gettempdir(), "mm_launcher.log")


def _log(msg: str) -> None:
    """写运行日志（pythonw 无控制台，日志落盘便于排查）。"""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def service_ready(timeout: float = 5.0) -> bool:
    """探测服务是否已能正常响应。

    ⚠️ 2026-09-28 修复：这里以前探的是**根路径**、超时只有 1.5 秒。
    实测根路径返回 index.html（FileResponse）在磁盘忙或冷启动时会慢到十几秒，
    于是被判成"服务没起来"；紧接着 port_in_use() 又为真，
    启动器就误报「端口 8000 已被其他程序占用」，把用户直接挡在门外。
    现在优先探轻量的 /api/health（纯内存、实测 0.03s），超时放宽到 5 秒，
    根路径只作为兜底。
    """
    for probe in (HEALTH_URL, URL):
        try:
            with urllib.request.urlopen(probe, timeout=timeout) as r:
                if 200 <= r.status < 400:
                    return True
        except Exception:
            continue
    return False


def port_in_use() -> bool:
    """端口是否有程序在监听（可能不是本助手）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


# 后端子进程的输出落这里（**不能丢 DEVNULL**）。
# 踩过两次：冷启动偶尔失败，界面上只弹一句"服务启动失败"，
# 而它为什么失败 —— 导入报错？端口被占？模型没就绪？一个字都看不到，
# 只能靠"手工把 run.py 跑一遍"去猜，非常浪费时间。
BACKEND_LOG = os.path.join(tempfile.gettempdir(), "mm_backend.log")


def start_service() -> subprocess.Popen:
    """后台拉起服务，CREATE_NO_WINDOW 确保不弹黑框控制台。

    设置 MM_NO_WINDOW=1：让 run.py 只跑后端服务、不要再自己开窗口，
    否则会和本启动器的窗口叠在一起（用户会看到两个窗口）。
    """
    # 便携包：先把数据/模型目录锚定到包内，再把**自带的** Ollama 拉起来。
    # 两件事都必须在 Popen 之前做完 —— 后端子进程是继承这份 env 的。
    bundle_hint = ensure_ollama() if ensure_bundle_env() else ""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    env = dict(os.environ)
    env["MM_NO_WINDOW"] = "1"
    env["PYTHONUNBUFFERED"] = "1"     # 否则日志要等进程退出才落盘，等于没有
    # 后端子进程默认按系统 locale（简中是 GBK）输出，而这个日志文件是 UTF-8 ——
    # 混着写就会变成"表头正常、后面全乱码"。统一成 UTF-8。
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        logf = open(BACKEND_LOG, "a", encoding="utf-8", errors="replace")
        logf.write(
            f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动后端 "
            f"{' '.join(SERVICE_CMD)} =====\n")
        if bundle_hint:
            logf.write(f"[便携包] {bundle_hint}\n")
            logf.write(f"[便携包] 包根={_BUNDLE_DIR}\n")
            logf.write(f"[便携包] 模型目录={_BUNDLE_MODELS}  数据目录={_BUNDLE_DATA}\n")
        logf.flush()
    except Exception:
        logf = subprocess.DEVNULL
    return subprocess.Popen(
        SERVICE_CMD,
        cwd=SERVICE_CWD,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,     # 报错也进同一个日志
        creationflags=creationflags,
    )


def wait_ready(proc: subprocess.Popen | None, timeout: float = 180.0) -> bool:
    """等待服务就绪；若子进程提前退出则立刻返回 False。

    超时给到 180 秒：这个后端要导入 torch 等重依赖，
    机器在忙（比如刚跑完 Docker 构建、杀软在扫盘）时**冷启动明显变慢** ——
    设 90 秒实测踩过：进程还活着、在正常导入，却被判超时，白弹一个失败框。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if service_ready():
            return True
        if proc is not None and proc.poll() is not None:
            _log(f"后端子进程提前退出，退出码 {proc.returncode}（详见 {BACKEND_LOG}）")
            return False
        time.sleep(0.4)
    _log(f"等待后端就绪超时（{timeout:.0f} 秒），详见 {BACKEND_LOG}")
    return False


def _filename_from_disposition(cd: str) -> str:
    """从 Content-Disposition 里抠出文件名（中文走 RFC 5987 的 filename* 形式）。"""
    cd = cd or ""
    m = re.search(r"filename[*]=utf-8''([^;]+)", cd, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        try:
            return m.group(1).encode("latin-1").decode("utf-8")
        except Exception:
            return m.group(1)
    return ""


def _default_save_dir() -> str:
    """默认落在「下载」文件夹，拿不到就退回用户主目录。"""
    try:
        if winreg:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as k:
                d = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
                if d and os.path.isdir(d):
                    return d
    except Exception:
        pass
    return os.path.expanduser("~")


class DownloadApi:
    """给页面用的一点点原生能力（目前只有「另存为」）。

    ⚠️ 为什么必须自己接管：
       桌面窗口是 WebView2，而 pywebview 的 settings['ALLOW_DOWNLOADS']
       **默认是 False** —— edgechromium 后端在 DownloadStarting 里直接
       `args.Cancel = True`，把页面发起的一切下载**静默取消**。
       表现就是用户点「点击下载」毫无反应、也没有任何提示，根本不知道成没成。
       （和「WebView2 不支持 window.prompt、静默返回 null」是同一类坑。）

    这里改成：从本机后端取字节 → 弹系统「另存为」→ 写盘 → 把结果回报给页面。
    这样界面能明确说出「存到哪了 / 你取消了 / 失败原因」，而不是一片沉默。
    """

    def __init__(self):
        # ⚠️ 一定要用**下划线**属性存窗口：pywebview 生成 JS 接口清单时会
        #    递归遍历本对象的**公开**属性（webview/util.py 的 get_functions），
        #    挂成 self.window 会让它顺着窗口对象一路爬进 WinForms 的
        #    native / AccessibilityObject / Bounds… 直接 RecursionError，
        #    启动日志里刷一长串 "Error while processing window.native..."。
        #    下划线开头的名字会被它跳过。
        self._window = None

    def save_file(self, path, filename=""):
        try:
            path = str(path or "")
            # 只允许本站 /api/ 下的地址：模型有可能随口编个外链，
            # 别让它把任意网址的内容写进用户磁盘。
            # 页面可能传绝对地址（前端做归一化时会带域名），取回 path+query 即可；
            # 但**只认本机自己** —— 别让页面（或模型编的外链）把任意网址的内容写进磁盘。
            if path.startswith(("http://", "https://")):
                sp = urllib.parse.urlsplit(path)
                if sp.netloc not in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT):
                    return {"ok": False, "error": "只支持本站的下载地址"}
                path = sp.path + (("?" + sp.query) if sp.query else "")
            if not path.startswith("/api/"):
                return {"ok": False, "error": "只支持本站 /api/ 下的下载地址"}

            # ⚠️ 模型写的链接里，文件名常常**没做百分号编码**（直接把中文写进 URL），
            #    而 http.client 发请求行时要按 ascii 编码 → UnicodeEncodeError
            #    （实测报错：'ascii' codec can't encode characters in position 29-34）。
            #    这里统一补一次；`%` 放进 safe 里，已经编码过的不会二次编码。
            path = urllib.parse.quote(
                path, safe="/?&=:%+#[]@!$'()*,;~")
            req = urllib.request.Request(URL + path,
                                         headers={"User-Agent": "mm-desktop"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    name = (str(filename or "").strip()
                            or _filename_from_disposition(
                                resp.headers.get("Content-Disposition"))
                            or "下载文件")
            except urllib.error.HTTPError as he:
                tip = {404: "文件不存在（可能已被删除或改名）",
                       403: "没有权限读取该文件"}.get(he.code, "服务端返回 %s" % he.code)
                return {"ok": False, "error": tip}
            if not data:
                return {"ok": False, "error": "服务端返回了 0 字节"}

            # webview 是在 main() 里才 import 的（模块级没有），这里就地取一次；
            # 取不到也不致命 —— pywebview 里 FileDialog.SAVE 的值恒为 30。
            try:
                import webview as _wv
                save_type = _wv.FileDialog.SAVE
            except Exception:
                save_type = 30

            target = self._window.create_file_dialog(
                save_type,
                directory=_default_save_dir(),
                save_filename=name,
            )
            if not target:
                # ⚠️ 返回 None 有两种可能：用户点了取消，**或者**对话框被抢占/没弹出来。
                #    实测"手快连点两下"时，第 2 个请求会立刻拿到 None ——
                #    日志里就成了 1 秒内三条"用户取消了"，看着像功能没生效。
                #    （前端已加防重入，这里把话说清楚，方便日后排查。）
                _log("另存为：没拿到路径（用户取消，或对话框被抢占）")
                return {"ok": False, "cancelled": True}
            dest = target[0] if isinstance(target, (list, tuple)) else str(target)
            with open(dest, "wb") as f:
                f.write(data)
            _log("另存为成功：%s（%d 字节）" % (dest, len(data)))
            return {"ok": True, "path": dest, "size": len(data)}
        except Exception as exc:
            _log("另存为失败：%s" % exc)
            return {"ok": False, "error": str(exc)}


def main() -> None:
    target = SERVICE_CMD[-1]
    if not os.path.exists(target):
        alert(f"未找到主程序：\n{target}\n\n请确认程序是否被移动或删除。")
        return

    proc = None

    if service_ready():
        pass  # 服务已在运行：直接复用，规避端口冲突
    elif port_in_use():
        # ⚠️ 2026-09-28：端口有监听 ≠ 是别的程序占的。本程序自己的后端在忙
        # （磁盘 IO 打满 / 模型预热 / 冷启动）时，探测会失败一次。直接下结论
        # 就会误报「端口被其他程序占用」，把用户挡在自己的程序外面（实测踩到）。
        # 所以再给一次机会：等几秒重探，仍不通才报错，并把两种可能都说清楚。
        ready = False
        for _ in range(6):          # 最多再等 ~9 秒
            time.sleep(1.5)
            if service_ready():
                ready = True
                break
        if not ready:
            alert(
                f"端口 {PORT} 被占用，但探测不到本助手服务。\n\n"
                f"可能的原因：\n"
                f"1) 本程序的上一个实例卡住了 —— 可执行：\n"
                f"   taskkill /F /IM pythonw.exe\n"
                f"   然后重新打开本程序；\n"
                f"2) 确实是别的程序占用了该端口 —— 可执行：\n"
                f"   netstat -ano | findstr :{PORT}"
            )
            return
    else:
        proc = start_service()
        if not wait_ready(proc):
            alert(
                "服务启动失败或超时。\n\n"
                f"后端日志（排查先看它）：\n{BACKEND_LOG}\n\n"
                "请检查：\n"
                "1) Ollama 是否已启动（模型 qwen3-vl:8b 是否就绪）\n"
                f"2) 端口 {PORT} 是否被占用\n"
                "3) 程序文件是否完整"
            )
            if proc.poll() is None:
                proc.terminate()
            return

    try:
        import webview
    except ImportError:
        alert(
            "缺少桌面窗口组件 pywebview。\n\n"
            "请在本目录执行：\n"
            'pip install pywebview'
        )
        return

    # ⚠️ 默认是 False：WebView2 会把页面发起的所有下载**静默取消**
    # （点「下载」毫无反应、也没有任何提示，就是这个原因）。
    # 开着它，页面万一走了普通 <a download> 也不会被吞掉；
    # 常规路径则由下面的 DownloadApi 接管（有系统对话框 + 结果回报）。
    try:
        webview.settings['ALLOW_DOWNLOADS'] = True
    except Exception as exc:
        _log("打开下载开关失败：%s" % exc)

    api = DownloadApi()
    window = webview.create_window(
        TITLE, URL,
        width=WINDOW_W, height=WINDOW_H,
        min_size=(900, 600),
        text_select=True,
        js_api=api,
    )
    api._window = window      # 下划线属性（见 DownloadApi 注释）

    def on_loaded():
        """页面加载完成后注入拖拽/粘贴增强（每次加载都会重新注入）。"""
        try:
            result = window.evaluate_js(INJECT_JS.replace("\n", " "))
            ready = window.evaluate_js("window.__mmDropPasteReady === true")
            selfcheck = window.evaluate_js(_SELFCHECK_JS)
            _log(f"注入结果={result} 就绪={ready} 自检={selfcheck}")
        except Exception as exc:
            _log(f"注入失败: {exc}")

    window.events.loaded += on_loaded
    webview.start()

    # 窗口关闭：只回收本次启动的服务，复用的服务保持不变
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
