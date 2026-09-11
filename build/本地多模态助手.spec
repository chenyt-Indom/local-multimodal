# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把后端 + 前端 + 本地 SD 模型打成桌面应用（onedir）。

- 独立桌桌面窗口：run.py 用 pywebview 打开，不依赖外部浏览器。
- 对话/记忆/文件/知识库：由本机 Ollama（qwen3-vl:8b）推理。
- 文生图/图片微改：内置 torch + diffusers + 本地 SD 模型（sd_model 目录）。
全部离线可用。模型 OLLama 由独立安装的 Ollama 提供。
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


# —— 收集文生图 / GUI 相关的大型第三方依赖 ——
_pkgs = ['torch', 'diffusers', 'transformers', 'accelerate', 'safetensors',
         'torchvision', 'tokenizers', 'huggingface_hub', 'webview']

all_datas, all_binaries, all_hidden = [], [], []
for _p in _pkgs:
    _d, _b, _h = _collect(_p)
    all_datas += _d
    all_binaries += _b
    all_hidden += _h

# —— transformers 运行时依赖校验所需的分发包元数据（dist-info）——
# transformers 启动时会用 importlib.metadata 校验 requests/filelock/numpy 等
# 依赖版本，缺这些 metadata 会在打包环境里报
# "No package metadata was found for The 'requests' distribution was not found"。
_meta_pkgs = ['requests', 'filelock', 'huggingface_hub', 'numpy', 'packaging',
              'pyyaml', 'regex', 'tokenizers', 'safetensors', 'tqdm',
              'torch', 'torchvision', 'accelerate']
for _p in _meta_pkgs:
    try:
        all_datas += copy_metadata(_p)
    except Exception:
        pass

# —— 前端与自带 SD 模型（只读打包资源）——
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


# 跳过 sd_turbo.safetensors.incomplete（未下载完的残片，约 693MB）
_SD_ROOT = r"D:\local-multimodal-models\sd-turbo"
all_datas += _dir_datas(os.path.join(root, "frontend"), "frontend")
all_datas += _dir_datas(_SD_ROOT, "sd_model", skip_suffixes=(".incomplete",))

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
    excludes=["tkinter", "PyQt5", "PySide6", "PyQt6"],
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
    upx=False,                 # 大体积 torch/CUDA 库不适合 upx 压缩，易损坏且极慢
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,             # 桌面应用：不弹黑框控制台（失败时由 run.py 弹窗提示）
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