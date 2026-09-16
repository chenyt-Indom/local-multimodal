# -*- coding: utf-8 -*-
"""把 mm-bridge 扩展打成 .vsix（它本质就是个 zip）。

用法：python build_mm_bridge.py [输出路径.vsix]
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "mm-bridge")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "mm-bridge.vsix")

# 顺序有讲究：`[Content_Types].xml` 要是 zip 里的**第一个**条目
FILES = [
    ("[Content_Types].xml", "[Content_Types].xml"),
    ("extension.vsixmanifest", "extension.vsixmanifest"),
    ("extension/package.json", "extension/package.json"),
    ("extension/extension.js", "extension/extension.js"),
]

missing = [s for s, _ in FILES if not os.path.isfile(os.path.join(SRC, s))]
if missing:
    raise SystemExit("缺少文件：%s" % missing)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for src, arc in FILES:
        z.write(os.path.join(SRC, src), arc)

print("✅ 打包完成：%s（%d 字节）" % (OUT, os.path.getsize(OUT)))
with zipfile.ZipFile(OUT) as z:
    for n in z.namelist():
        print("   ", n)
