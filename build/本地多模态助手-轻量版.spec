# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（轻量版）：对话 + 图片理解 + Agent 工具链。

与完整版的区别：
- 不打包 torch / diffusers / transformers 等文生图依赖（约 -6.6GB）
- 不打包随应用分发的 SD 模型 sd_model（约 -4.0GB）
- 体积从约 11GB 降到约 500MB，适合上传 GitHub Releases 分发

说明：文生图/图片微改在轻量版中不可用（调用时会提示缺少依赖），
      其余功能（对话、图片理解、记忆、知识库、文件工具、联网工具、实时时间）均正常。
后端对 torch 采用函数内延迟导入，因此缺少该依赖不会影响启动。
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = os.path.abspath(os.getcwd())


def _collect(pkg):
    try:
        datas, binaries, hidden = collect_all(pkg)
        return datas, binaries, hidden
    except Exception:
        return [], [], [pkg]


# —— 只收集 GUI 相关依赖（不含文生图大件）——
_pkgs = ['webview']

all_datas, all_binaries, all_hidden = [], [], []
for _p in _pkgs:
    _d, _b, _h = _collect(_p)
    all_datas += _d
    all_binaries += _b
    all_hidden += _h

# —— 关键分发包元数据（部分库启动时会校验依赖版本）——
_meta_pkgs = ['requests', 'filelock', 'numpy', 'packaging', 'pyyaml', 'tqdm']
for _p in _meta_pkgs:
    try:
        all_datas += copy_metadata(_p)
    except Exception:
        pass


# —— 前端资源（不含 SD 模型）——
def _dir_datas(src_root, dst_root, skip_suffixes=()):
    """把目录展开为 (文件, 目标目录) 列表；可跳过指定后缀的残余文件。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for fn in filenames:
            if any(fn.endswith(s) for s in skip_suffixes):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(dirpath, src_root)
            dst = dst_root if rel == "." else os.path.join(dst_root, rel)
            out.append((src, dst))
    return out


all_datas += _dir_datas(os.path.join(root, "frontend"), "frontend")

# —— 后端模块的显式收集 ——
all_hidden += [
    "backend.main", "backend.config", "backend.ollama_client", "backend.tools",
    "backend.t2i", "backend.memory", "backend.kb", "backend.file_tools",
    "backend.video", "backend.web_tools",
    "uvicorn", "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "fastapi", "pydantic", "websockets", "multipart", "python_multipart",
    "clr", "pythonnet",
]
all_hidden += collect_submodules("uvicorn", filter=lambda n: True)

a = Analysis(
    [os.path.join(root, "run.py")],
    pathex=[root],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "PyQt5", "PySide6", "PyQt6",
        # 文生图相关大件（轻量版不带）
        "torch", "torchvision", "diffusers", "transformers",
        "accelerate", "safetensors", "tokenizers", "huggingface_hub",
        "matplotlib", "pandas", "IPython", "notebook", "jupyter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="本地多模态助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="本地多模态助手",
)
