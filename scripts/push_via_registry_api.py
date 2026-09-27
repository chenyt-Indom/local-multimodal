# -*- coding: utf-8 -*-
"""把当前 `models/` 的内容**直接推**到 CCR 的 multimodal-models 镜像（不经过 Docker）。

## 为什么要自己写（而不是 `docker push`）
1. `docker push` **没有分块上传**。单层 18.56GB 的 PUT 累计失败 6 次
   （`broken pipe` / `use of closed network connection`，都经 Docker Desktop 代理）。
2. 走 buildx 重建也不行：72GB 构建上下文从 Windows 客户端喂给守护进程，实测 **0.9 MB/s**（≈22 小时）。
3. 实测**宿主机原生进程**直连仓库可行，且 Registry V2 支持**分块 PATCH**：
   64MB 实测 **3.60 MB/s**。

## 做法（外科手术式更新，不用重建镜像）
- 保留仓库里现有的 **4 个基础层**（debian/python：让镜像里仍有 `sh`，部署脚本要用）。
- 把它们后面的**模型层全部替换**成当前 `models/` 的内容（旧的 sd-turbo / 8B / 14B 一并换掉）。
- 层内路径沿用仓库既有规范：`models/...`（无前导斜杠）。
- **全部分块上传完成后**才 PUT manifest —— 中途任何失败都不影响 `latest` 仍是可用的旧版。

## 安全
- 原 manifest / config 已备份在 `mm-push/`（见 `manifest.original.json`）。
- 每个 blob 上传前先 `HEAD` 探测：已存在就跳过 ⇒ **脚本可直接重跑续传**。
- `--dry-run` 只算不传；`--canary` 用极小层验证链路。
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

# ============================================================
# 可配置项（改这几个就够了）
#   REG / REPO       目标仓库
#   MODELS           要上传的模型目录（默认取交付包根下的 models/）
#   TMPDIR           打包用的临时目录（**别放 C 盘**，单层最大 19.5GB）
#   KEEP_BASE_LAYERS 保留仓库里最前面 N 个"基础层"（debian/python）——
#                    这样镜像里仍有 sh / python3，部署脚本的 `docker run ... sh -c` 才能用
#   GZ_LEVEL         打包压缩级别。模型权重本身已压过，level 1 反而把
#                    18.56GB 变成 19.50GB（+5%）；理论上 level 0（store）最省时间，
#                    但必须与已上传的层保持一致，否则 digest 变了要重传。
#   LOG              进度日志
# ============================================================

REG = "ccr.ccs.tencentyun.com"
REPO = "bendiai/multimodal-models"
MODELS = r"E:/本地多模态助手-Docker/models"
TMPDIR = r"E:/mm-upload-tmp"          # 临时 tar.gz 落盘位置（E 盘，别用只剩 20G 的 C）
KEEP_BASE_LAYERS = 4                   # 仓库里前 4 层是 debian/python 基础层，保留
BIG = 64 * 1024 * 1024                 # 大于它 → 单独一层
CHUNK = 32 * 1024 * 1024
GZ_LEVEL = 1

LOG = r"C:/Users/19853/AppData/Local/Temp/push_models_custom.log"


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def hdr(hd, name):
    for k, v in hd.items():
        if k.lower() == name.lower():
            return v
    return None


def go(url, method="GET", data=None, headers=None, timeout=900):
    r = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_auth():
    p = subprocess.run(["docker-credential-desktop", "get"], input=REG.encode(),
                       capture_output=True)
    d = json.loads(p.stdout.decode("utf-8", "replace"))
    basic = "Basic " + base64.b64encode(
        ("%s:%s" % (d["Username"], d["Secret"])).encode()).decode()
    q = "?service=token-service&scope=" + urllib.parse.quote(
        "repository:%s:pull,push" % REPO)
    st, _, b = go("https://%s/service/token%s" % (REG, q), headers={"Authorization": basic})
    if st != 200:
        raise SystemExit("取 token 失败 HTTP %s" % st)
    return {"Authorization": "Bearer " + json.loads(b.decode())["token"]}


# ---------------- 层规划 ----------------
def plan_layers():
    """按「大文件各自一层、小文件按所在目录合并」规划层，返回 [(标题, [绝对路径...]), ...]"""
    big, groups = [], {}
    for root, _dirs, files in os.walk(MODELS):
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, MODELS).replace("\\", "/")
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz >= BIG:
                big.append((sz, fp, rel))
            else:
                groups.setdefault(os.path.dirname(rel), []).append((sz, fp, rel))
    big.sort(reverse=True)   # 大文件按体积降序：先传最占时间的，进度最有代表性
    # ⚠️ 每个大文件**各自一层**：层越小 → 单次失败代价越小、
    #    重跑时已存在的 blob 会秒跳过（断点续传）。
    layers = [([p], r) for _s, p, r in big]
    for d, items in groups.items():
        layers.append(([p for _s, p, _r in items], "目录 %s" % (d or ".")))
    # 逐层体积
    out = []
    for paths, title in layers:
        out.append((title, sorted(paths)))
    return out


def build_layer_tgz(paths, out_path):
    """把一组文件打成一个 tar.gz；返回 (tar 字节数, gz 字节数, diffID, blob_digest)"""
    h_tar = hashlib.sha256()
    h_gz = hashlib.sha256()
    n_tar = [0]

    class Tee(io.RawIOBase):
        def __init__(self, fh):
            self.fh = fh

        def writable(self):
            return True

        def write(self, b):
            h_tar.update(b)
            n_tar[0] += len(b)
            self.fh.write(b)
            return len(b)

    with open(out_path, "wb") as raw:
        gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=GZ_LEVEL)
        tee = Tee(gz)
        with tarfile.open(fileobj=tee, mode="w|") as t:
            for p in paths:
                rel = os.path.relpath(p, MODELS).replace("\\", "/")
                ti = t.gettarinfo(p, arcname="models/" + rel)
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with open(p, "rb") as fh:
                    t.addfile(ti, fh)
        gz.close()
    n_gz = os.path.getsize(out_path)
    with open(out_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h_gz.update(chunk)
    return n_tar[0], n_gz, "sha256:" + h_tar.hexdigest(), "sha256:" + h_gz.hexdigest()


def upload_file(auth, path, digest, size):
    st, _, _ = go("https://%s/v2/%s/blobs/%s" % (REG, REPO, digest), method="HEAD", headers=auth)
    if st == 200:
        return True, 0.0
    st, hd, b = go("https://%s/v2/%s/blobs/uploads/" % (REG, REPO), method="POST", headers=auth)
    if st not in (202, 201):
        raise RuntimeError("开上传会话失败 HTTP %s %s" % (st, b[:160]))
    loc = hdr(hd, "Location") or ""
    if loc.startswith("/"):
        loc = "https://%s%s" % (REG, loc)
    sent, t0, last_log = 0, time.time(), 0.0
    with open(path, "rb") as f:
        while sent < size:
            piece = f.read(CHUNK)
            if not piece:
                break
            h = dict(auth)
            h["Content-Type"] = "application/octet-stream"
            h["Content-Length"] = str(len(piece))
            h["Content-Range"] = "%d-%d" % (sent, sent + len(piece) - 1)
            st, hd2, b2 = go(loc, method="PATCH", data=piece, headers=h, timeout=3600)
            if st not in (202, 201):
                raise RuntimeError("PATCH 失败 HTTP %s %s" % (st, b2[:160]))
            loc = hdr(hd2, "Location") or loc        # ★ 必须用响应里新的 Location
            if loc.startswith("/"):
                loc = "https://%s%s" % (REG, loc)
            sent += len(piece)
            el = time.time() - t0
            if el - last_log >= 120:                 # 每 2 分钟报一次进度
                last_log = el
                log("       … 已传 %.2f/%.2f GB  (%.2f MB/s)"
                    % (sent / 1e9, size / 1e9, sent / el / 1e6))
    sep = "&" if "?" in loc else "?"
    st, _, b = go(loc + sep + "digest=" + urllib.parse.quote(digest), method="PUT",
                  headers=dict(auth, **{"Content-Length": "0"}), timeout=1800)
    if st not in (201, 202):
        raise RuntimeError("提交 blob 失败 HTTP %s %s" % (st, b[:160]))
    return False, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--canary", action="store_true")
    args = ap.parse_args()

    os.makedirs(TMPDIR, exist_ok=True)
    auth = get_auth()
    log("已取得仓库 token")

    st, _, b = go("https://%s/v2/%s/manifests/latest" % (REG, REPO),
                  headers=dict(auth, Accept="application/vnd.oci.image.manifest.v1+json"))
    base = json.loads(b)
    st, _, cb = go("https://%s/v2/%s/blobs/%s" % (REG, REPO, base["config"]["digest"]),
                   headers=auth)
    cfg = json.loads(cb)
    log("当前 latest：%d 层；config diff_ids=%d"
        % (len(base["layers"]), len(cfg["rootfs"]["diff_ids"])))
    base_layers = base["layers"][:KEEP_BASE_LAYERS]
    base_diffs = cfg["rootfs"]["diff_ids"][:KEEP_BASE_LAYERS]
    # 基础层对应的 history 前缀：取到"第一个非空层的计数达到 KEEP_BASE_LAYERS"为止
    hist_prefix, n = [], 0
    for e in cfg.get("history", []):
        hist_prefix.append(e)
        if not e.get("empty_layer"):
            n += 1
            if n >= KEEP_BASE_LAYERS:
                break
    log("保留基础层 %d 层 + history 前缀 %d 条（保留 sh，部署脚本要用）"
        % (len(base_layers), len(hist_prefix)))

    if args.canary:
        layers = [("canary", None)]      # 特判
    else:
        layers = plan_layers()
    total = 0
    for title, paths in layers:
        if paths is None:
            total += 10240
        else:
            total += sum(os.path.getsize(p) for p in paths)
    log("计划上传 %d 个新层，内容合计 %.2f GB" % (len(layers), total / 1e9))
    if args.dry_run:
        for title, paths in layers:
            if paths is None:
                log("   %-24s 10 KB" % title)
                continue
            s = sum(os.path.getsize(p) for p in paths)
            log("   %-24s %8.2f GB  %d 个文件" % (title, s / 1e9, len(paths)))
        log("干跑结束，未上传任何东西")
        return

    new_layers, new_diffs, new_hist = [], [], []
    t_start = time.time()
    for i, (title, paths) in enumerate(layers, 1):
        tmp = os.path.join(TMPDIR, "layer%02d.tar.gz" % i)
        if paths is None:                # canary
            payload = b"transport ok %d\n" % int(time.time())
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                ti = tarfile.TarInfo("models/.canary")
                ti.size, ti.mtime, ti.mode = len(payload), int(time.time()), 0o644
                t.addfile(ti, io.BytesIO(payload))
            raw = buf.getvalue()
            comp = gzip.compress(raw, 6)
            open(tmp, "wb").write(comp)
            n_tar, n_gz = len(raw), len(comp)
            diff = "sha256:" + hashlib.sha256(raw).hexdigest()
            blob = "sha256:" + hashlib.sha256(comp).hexdigest()
        else:
            n_tar, n_gz, diff, blob = build_layer_tgz(paths, tmp)
        log("[%d/%d] %s：tar %.2f GB → gz %.2f GB，开始上传…"
            % (i, len(layers), title, n_tar / 1e9, n_gz / 1e9))
        existed, dt = upload_file(auth, tmp, blob, n_gz)
        if existed:
            log("       已存在，秒跳过")
        else:
            log("       ✅ 上传完成 %.2f GB / %.0fs = %.2f MB/s"
                % (n_gz / 1e9, dt, n_gz / dt / 1e6))
        try:
            os.remove(tmp)
        except OSError:
            pass
        new_layers.append({"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                           "size": n_gz, "digest": blob})
        new_diffs.append(diff)
        new_hist.append({"created": "2026-09-28T00:00:00Z",
                         "created_by": "COPY models/... (%s) # models-refresh" % title[:40]})

    # ---- 全部层就绪，才写 config + manifest ----
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["rootfs"]["diff_ids"] = base_diffs + new_diffs
    cfg2["history"] = hist_prefix + new_hist
    cbytes = json.dumps(cfg2, separators=(",", ":")).encode()
    cdig = "sha256:" + hashlib.sha256(cbytes).hexdigest()
    log("上传新 config（%d 字节）…" % len(cbytes))
    tmpc = os.path.join(TMPDIR, "config.json")
    open(tmpc, "wb").write(cbytes)
    upload_file(auth, tmpc, cdig, len(cbytes))
    os.remove(tmpc)

    m2 = {"schemaVersion": 2,
          "mediaType": base.get("mediaType", "application/vnd.oci.image.manifest.v1+json"),
          "config": {"mediaType": base["config"].get(
              "mediaType", "application/vnd.oci.image.config.v1+json"),
              "size": len(cbytes), "digest": cdig},
          "layers": base_layers + new_layers}
    body = json.dumps(m2, separators=(",", ":")).encode()
    tag = "models-refresh-canary" if args.canary else "latest"
    st, _, b = go("https://%s/v2/%s/manifests/%s" % (REG, REPO, tag), method="PUT",
                  data=body, headers=dict(auth, **{
                      "Content-Type": m2["mediaType"]}))
    log("提交 manifest [%s] → HTTP %s" % (tag, st))
    if st not in (201, 202):
        log("  ✘ 失败：%s" % b[:300].decode("utf-8", "replace"))
        return
    log("✅ 完成：%s 现在有 %d 层，总耗时 %.1f 分钟"
        % (tag, len(m2["layers"]), (time.time() - t_start) / 60))


main()
