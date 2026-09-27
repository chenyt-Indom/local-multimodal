#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核对离线交付包 images/*.tar 里的应用代码，是否与当前源码一致。

用法:
    python scripts/check_tar_content.py <tar 路径> [要核对的容器内路径 ...]

默认核对：
    /opt/app/backend/main.py
    /opt/app/backend/t2i.py
    /opt/app/backend/config.py
    /opt/app/frontend/app.js

判据：tar 里这些文件的 sha256 必须与源码仓库里的对应文件一致
（镜像里的路径去掉 /opt/app/ 前缀就是仓库相对路径）。

⚠️⚠️ 2026-09-27 踩过的两个坑（都让"其实是对的 tar"被判成"旧代码"）：

  1. **必须按真实层序取最上面那一层**，不能按 tar 里 blob 的物理顺序。
     同一个文件常同时存在于多个层（基础层一份 + 后来的补丁层一份），
     运行时 overlayfs 是上层覆盖下层，生效的是**层序最后**的那份。
     第一版取"遇到就停"，遇到基础层的旧文件就返回了 → 白重打 3GB tar。
     tar 里 `blobs/sha256/<hash>` 的排列顺序**不等于**层的上下顺序，
     唯一可靠的办法是顺着 `index.json → manifest.layers[]` 走。

  2. **镜像里有多份是正常的**（补丁层造成的），不是错误。
     脚本会把"出现在 N 层"标出来，方便一眼看出是补丁叠加还是真的写重了。
"""
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TARGETS = [
    "/opt/app/backend/main.py",
    "/opt/app/backend/t2i.py",
    "/opt/app/backend/config.py",
    "/opt/app/frontend/app.js",
]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _digest_to_path(digest: str) -> str:
    """sha256:abc… -> blobs/sha256/abc…"""
    algo, _, hexd = digest.partition(":")
    return "blobs/%s/%s" % (algo or "sha256", hexd)


class TarImage:
    """以只读方式访问一个 docker save 出来的 tar（OCI 布局）。"""

    def __init__(self, tar_path):
        self.t = tarfile.open(tar_path, "r:")

    def close(self):
        try:
            self.t.close()
        except Exception:
            pass

    def read_blob(self, name: str):
        """按 tar 成员名读取一个 blob 的原始字节；不存在返回 None。"""
        try:
            m = self.t.getmember(name)
        except KeyError:
            return None
        if not m.isfile():
            return None
        f = self.t.extractfile(m)
        return f.read() if f is not None else None

    def layer_names(self):
        """按**真实层序**（下 → 上）返回 layer blob 的 tar 成员名。

        顺着 index.json → manifest 走；任何一步读不通就退回"扫描全部
        blobs/sha256"，并明确告知顺序不可靠。
        """
        raw = self.read_blob("index.json")
        if raw is None:
            return self._fallback_layers(), False
        try:
            idx = json.loads(raw.decode("utf-8"))
            desc = (idx.get("manifests") or [{}])[0]
            digest = desc.get("digest")
            if not digest:
                raise ValueError("index.json 里没有 manifests[0].digest")

            man = json.loads(self.read_blob(_digest_to_path(digest)).decode("utf-8"))

            # 有时这一层还是 image index（多架构），再往里走一层
            if "manifests" in man and "layers" not in man:
                subs = man.get("manifests") or []
                pick = None
                for s in subs:
                    plat = (s.get("platform") or {})
                    if plat.get("os") == "linux" and plat.get("architecture") == "amd64":
                        pick = s
                        break
                pick = pick or (subs[0] if subs else None)
                if pick is None:
                    raise ValueError("内层 manifest 为空")
                man = json.loads(
                    self.read_blob(_digest_to_path(pick["digest"])).decode("utf-8"))

            layers = man.get("layers") or []
            if not layers:
                raise ValueError("manifest 里没有 layers")
            return [_digest_to_path(x["digest"]) for x in layers], True
        except Exception as e:
            print("  [warn] 读 index.json/manifest 失败（%s），改用物理扫描" % e)
            return self._fallback_layers(), False

    def _fallback_layers(self):
        """物理扫描：所有 gzip 的 blobs/sha256/*。顺序不可靠，仅兜底。"""
        out = []
        for m in self.t.getmembers():
            if not (m.name.startswith("blobs/sha256/") and m.isfile()):
                continue
            f = self.t.extractfile(m)
            if f is None:
                continue
            head = f.read(2)
            f.seek(0)
            if head == b"\x1f\x8b":
                out.append(m.name)
        return out


def scan_layers(img, targets):
    """按真实层序扫描，返回 ({后缀: (来源说明, bytes)}, {后缀: 出现次数})。"""
    names, ordered = img.layer_names()
    found, seen = {}, {}
    for idx, name in enumerate(names, 1):
        raw = img.read_blob(name)
        if raw is None:
            continue
        try:
            inner = tarfile.open(fileobj=gzip.GzipFile(fileobj=io.BytesIO(raw)),
                                 mode="r:")
        except Exception:
            continue                      # 不是 gzip（可能是配置 blob）
        try:
            for m in inner.getmembers():
                if not m.isfile():
                    continue
                for suf in targets:
                    if not (m.name == suf.lstrip("/") or m.name.endswith(suf)):
                        continue
                    ef = inner.extractfile(m)
                    if ef is None:
                        continue
                    # ⚠️ 覆盖写入 —— 后扫到的层压住先前的，最终留最上层
                    found[suf] = (
                        "%s第%d/%d层::%s" % ("" if ordered else "（层序不可靠）",
                                             idx, len(names), m.name),
                        ef.read())
                    seen[suf] = seen.get(suf, 0) + 1
        finally:
            inner.close()
    return found, seen


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tar_path = sys.argv[1]
    targets = sys.argv[2:] or DEFAULT_TARGETS
    print("tar: %s" % tar_path)
    print("size: %.1f MB" % (os.path.getsize(tar_path) / 1024 / 1024))

    img = TarImage(tar_path)
    try:
        names, ordered = img.layer_names()
        print("层数: %d %s" % (len(names), "" if ordered else "（层序不可靠，按物理顺序）"))
        found, seen = scan_layers(img, targets)
    finally:
        img.close()

    all_ok = True
    for tgt in targets:
        rel = tgt.replace("/opt/app/", "")
        local = os.path.join(REPO, rel.replace("/", os.sep))
        if tgt not in found:
            print("  [MISS] %-34s 不在 tar 里" % tgt)
            all_ok = False
            continue
        member, data = found[tgt]
        if not os.path.exists(local):
            print("  [SKIP] %-34s 本地无此文件（%s）" % (tgt, local))
            continue
        h_tar, h_loc = sha(data), sha(open(local, "rb").read())
        same = h_tar == h_loc
        all_ok = all_ok and same
        dup = "" if seen.get(tgt, 1) <= 1 else "   ⚠️ 共出现在 %d 层" % seen[tgt]
        print("  [%s] %-34s tar=%s local=%s%s"
              % ("OK  " if same else "DIFF", tgt, h_tar[:12], h_loc[:12], dup))
        if not same:
            print("         ↳ 取自 %s" % member)

    print("\n判据：按真实层序取**最上层**（运行时生效的那份）比 sha256。")
    print("结论: %s" % ("一致 ✅" if all_ok else "不一致 ❌ —— 需要重打 tar"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
