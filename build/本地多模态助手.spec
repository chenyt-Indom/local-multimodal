# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把后端 + 前端打成单文件可执行程序。
   打包后运行 exe 会自动启动本地服务并打开浏览器。
   模型推理由独立安装的 Ollama 完成，因此本程序很轻量。
"""
import os

root = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join(root, "run.py")],
    pathex=[root],
    binaries=[],
    datas=[
        (os.path.join(root, "frontend"), "frontend"),   # 前端静态资源
    ],
    hiddenimports=["backend.main", "backend.ollama_client", "backend.config"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="本地多模态助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # 保留控制台可查看日志；如需 GUI 无窗口可改为 False
    icon=None,
)