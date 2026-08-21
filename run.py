# -*- coding: utf-8 -*-
"""本地多模态助手 —— 启动入口
安装环境后直接运行:  py -3 run.py
会自动打开浏览器访问本地界面。
"""
import os
import threading
import time
import webbrowser

import uvicorn

from backend.ollama_client import OllamaError, OllamaClient
from backend import config

HOST = "127.0.0.1"
PORT = 8000


def _open_browser():
    """等后端就绪后打开浏览器。"""
    url = f"http://{HOST}:{PORT}/"
    for _ in range(50):
        try:
            if OllamaClient().health()["online"]:
                break
        except OllamaError:
            pass
        time.sleep(0.2)
    webbrowser.open(url)


if __name__ == "__main__":
    config.load_config()  # 确保配置文件生成
    t = threading.Thread(target=_open_browser, daemon=True)
    t.start()
    uvicorn.run("backend.main:app", host=HOST, port=PORT, log_level="info")