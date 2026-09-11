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

    try:
        import webview  # 延迟导入，避免源码环境未安装时阻塞后端
    except ImportError as exc:
        _alert("缺少桌面窗口组件 pywebview：\n" + str(exc))
        raise

    window = webview.create_window(
        "本地多模态助手",
        URL,
        width=1280,
        height=840,
        min_size=(960, 640),
        text_select=True,
    )
    try:
        webview.start(private_mode=False)
    finally:
        server.should_exit = True
        t.join(timeout=5)


if __name__ == "__main__":
    main()