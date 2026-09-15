# -*- coding: utf-8 -*-
"""本地多模态助手 —— 桌面启动入口

双击 exe（或 `py -3 run.py`）即启动：
1. 在后台线程拉起本地 FastAPI 服务（uvicorn）
2. 用 pywebview 弹出独立的桌面应用窗口，内嵌加载本地界面（不依赖外部浏览器）
3. 窗口关闭后自动回收后台服务线程，干净退出。

模型推理由本机 Ollama（默认 qwen3-vl:8b）完成；文生图/图片微改由随应用
分发的本地 SD 模型（sd_model 目录）完成，全程离线。
"""
import os
import threading
import time
import urllib.request

import uvicorn

from backend import config
from backend.main import app

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}/"

# 由外部启动器（桌面快捷方式用的「启动窗口.pyw」）拉起时置 1：
# 此时本进程只负责后端服务，窗口交给启动器显示，避免弹出两个窗口。
NO_WINDOW = os.environ.get("MM_NO_WINDOW") == "1"


def _serve(server: uvicorn.Server) -> None:
    """在后台线程运行 uvicorn 服务。"""
    config.load_config()  # 确保配置文件生成
    server.run()


def _wait_until_up(timeout: float = 60.0) -> bool:
    """轮询等待后端就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _alert(msg: str, title: str = "本地多模态助手") -> None:
    """无控制台时用系统弹窗提示失败原因，避免"闪退"且没有任何反馈。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        print(msg)


# --------------------------------------------------------------------------
#  语音唤醒：把窗口弹到最前
# --------------------------------------------------------------------------
WINDOW_TITLE = "本地多模态助手"


def _focus_window_win32() -> bool:
    """按标题找到应用窗口并置前（不依赖 pywebview 对象，因此对
    "窗口由外部启动器创建"的情况也有效）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if WINDOW_TITLE in (buf.value or ""):
                found.append(hwnd)
                return False        # 找到一个就停
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        # 最小化了要先还原，否则置前只能看到任务栏闪烁
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        else:
            user32.ShowWindow(hwnd, 5)      # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _make_wake_focus(window=None):
    """生成"被唤醒时"的回调：先试 pywebview 对象，再退回 Win32 按标题找。"""
    def _focus() -> None:
        if window is not None:
            try:
                window.show()
                window.restore()
            except Exception:
                pass
        _focus_window_win32()
    return _focus


def _autostart_voice() -> None:
    """启动时自动打开麦克风监听（可被配置项 voice_auto_start 关掉）。

    容器里没有麦克风，这里会失败 —— 失败就静默跳过，界面上点 🎤 会给出提示，
    不能让"没有麦克风的部署"因为自动启动而报错。
    """
    try:
        from backend import config as _cfg
        if not _cfg.load_config().get("voice_auto_start", True):
            return
        from backend import main as _main
        # 模型要在后台加载，稍等一下再开，避免和 ollama 抢资源
        time.sleep(3)
        r = _main.voice_start()
        print(f"[voice] 自动监听：{r}")
    except Exception as e:
        print(f"[voice] 自动监听失败（可忽略）：{e}")


def main() -> None:
    config.load_config()
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT,
                                           log_level="warning"))
    t = threading.Thread(target=_serve, args=(server,), daemon=True)
    t.start()

    if not _wait_until_up():
        # 服务起不来就退，避免白弹一个空白窗口
        server.should_exit = True
        _alert("本地服务启动失败。\n\n请依次检查：\n"
               "1) Ollama 是否已安装并正在运行\n"
               "2) 端口 8000 是否被其他程序占用\n"
               "3) 是否已拉取模型 qwen3-vl:8b")
        raise SystemExit("本地服务启动失败")

    if NO_WINDOW:
        # 被外部启动器拉起：这里只保持后端服务存活，窗口由启动器负责，
        # 否则会和启动器各开一个窗口（用户看到两个）。
        # 窗口不由本进程创建，所以"唤醒置前"只能靠 Win32 按标题找。
        try:
            from backend import voice as _voice
            _voice.set_wake_hook(_make_wake_focus(None))
        except Exception:
            pass
        threading.Thread(target=_autostart_voice, daemon=True,
                         name="voice-autostart").start()
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            t.join(timeout=5)
        return

    try:
        import webview  # 延迟导入，避免源码环境未安装时阻塞后端
    except ImportError as exc:
        _alert("缺少桌面窗口组件 pywebview：\n" + str(exc))
        raise

    window = webview.create_window(
        WINDOW_TITLE,
        URL,
        width=1280,
        height=840,
        min_size=(960, 640),
        text_select=True,
    )
    # 语音唤醒时要能把这个窗口弹到最前 —— 在这里注册回调
    try:
        from backend import voice as _voice
        _voice.set_wake_hook(_make_wake_focus(window))
    except Exception:
        pass
    threading.Thread(target=_autostart_voice, daemon=True,
                     name="voice-autostart").start()
    try:
        webview.start(private_mode=False)
    finally:
        server.should_exit = True
        t.join(timeout=5)


if __name__ == "__main__":
    main()