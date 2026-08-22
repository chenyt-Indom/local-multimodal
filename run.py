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


def main() -> None:
    config.load_config()
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT,
                                           log_level="warning"))
    t = threading.Thread(target=_serve, args=(server,), daemon=True)
    t.start()

    if not _wait_until_up():
        # 服务起不来就退，避免白弹一个空白窗口
        server.should_exit = True
        raise SystemExit("本地服务启动失败")

    import webview  # 延迟导入，避免源码环境未安装时阻塞后端
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