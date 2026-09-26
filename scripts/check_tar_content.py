#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核对离线交付包 images/*.tar 里的应用代码，是否与当前源码一致。

用法:
    python scripts/check_tar_content.py <tar 路径> [要核对的容器内路径 ...]

默认核对：
    /opt/app/backend/main.py
    /opt/app/backend/t2i.py
    /opt/app/frontend/app.js

判据：tar 里这些文件的 sha256 必须与源码仓库里的对应文件一致
（镜像里的路径去掉 /opt/app/ 前缀就是仓库相对路径）。
"""
import hashlib
import io
import json
import os
import sys
import tarfile
import gzip

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TARGETS = [
    "/opt/app/backend/main.py",
    "/opt/app/backend/t2i.py",
    "/opt/app/backend/config.py",
    "/opt/app/frontend/app.js",
]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def iter_layer_blobs(tar_path):
    """产出 (name, fileobj) —— OCI 布局里所有 gzip 压缩的 layer blob。"""
    with tarfile.open(tar_path, "r:") as t:
        for m in t.getmembers():
            if m.name.startswith("blobs/sha256/") and m.isfile():
                f = t.extractfile(m)
                if f is None:
                    continue
                head = f.read(2)
                f.seek(0)
                if head == b"\x1f\x8b":  # gzip
                    yield m.name, f


def find_in_tar(tar_path, wanted_suffixes):
    """在 tar 的所有 layer 中查找以给定后缀结尾的文件，返回 {后缀: (成员名, bytes)}。"""
    found = {}
    for blob_name, f in iter_layer_blobs(tar_path):
        try:
            gz = gzip.GzipFile(fileobj=f)
            with tarfile.open(fileobj=gz, mode="r:") as inner:
                for m in inner.getmembers():
                    if not m.isfile():
                        continue
                    for suf in wanted_suffixes:
                        if suf in found:
                            continue
                        if m.name == suf.lstrip("/") or m.name.endswith(suf):
                            ef = inner.extractfile(m)
                            if ef is None:
                                continue
                            found[suf] = (blob_name + "::" + m.name, ef.read())
        except Exception as e:
            print("  [warn] 跳过 %s: %s" % (blob_name, e))
        if len(found) == len(wanted_suffixes):
            break
    return found


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tar_path = sys.argv[1]
    targets = sys.argv[2:] or DEFAULT_TARGETS
    print("tar: %s" % tar_path)
    print("size: %.1f MB" % (os.path.getsize(tar_path) / 1024 / 1024))
    found = find_in_tar(tar_path, targets)

    all_ok = True
    for tgt in targets:
        # 镜像内路径 -> 仓库相对路径
        rel = tgt.replace("/opt/app/", "")
        local = os.path.join(REPO, rel.replace("/", os.sep))
        if tgt not in found:
            print("  [MISS] %-34s 不在 tar 里" % tgt)
            all_ok = False
            continue
        member, data = found[tgt]
        h_tar = sha(data)
        if not os.path.exists(local):
            print("  [SKIP] %-34s 本地无此文件（%s）" % (tgt, local))
            continue
        h_loc = sha(open(local, "rb").read())
        same = h_tar == h_loc
        all_ok = all_ok and same
        print("  [%s] %-34s tar=%s local=%s"
              % ("OK  " if same else "DIFF", tgt, h_tar[:12], h_loc[:12]))
    print("\n结论: %s" % ("一致 ✅" if all_ok else "不一致 ❌ —— 需要重打 tar"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
