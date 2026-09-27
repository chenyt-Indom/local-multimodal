# -*- coding: utf-8 -*-
"""给模型数据镜像生成**按文件分层**的 Dockerfile（替代 `COPY models /models` 单大层）。

## 为什么要拆层（2026-09-27 实测，三次翻车换来的）

原来只有一个 `COPY models /models` → **一个 50.9GB 的大层**。
往腾讯云镜像仓库推的时候，**连续三次**都在最后一步失败：

```
failed commit on ref "layer-sha256:f215ef66e9dd…":
  Put "…/blobs/uploads/<uuid>?…&digest=sha256%3Af215…":
  net/http: timeout awaiting response headers        ← 每次都是跑满 ~2 小时才报
```

也就是说：数据全传上去了，**最后那个"提交"请求等不到响应**（三次同样位置）。
官方文档里给的是「单层上传 20MB/s、上传并发数 5、多并发 = min(COS带宽/并发,20MB/s)」——
**registry 是支持多并发的**，那就没有理由把 50GB 压在一个层里：

| | 单大层 | 按文件分层 |
|---|---|---|
| 最大单层 | 50.9 GB | **18.56 GB**（一个 ollama 权重） |
| 上传并发 | 1 | 最多 5（docker 默认） |
| 失败代价 | 全部重传（~2 小时） | 只重传那一层 |
| 重试能否续传 | 不能（层没提交就等于没有） | **能**（已提交的层会 `Already exists` 跳过） |

## 用法

    python make_models_layers.py                # 只生成 Dockerfile 到 /tmp/mm-models-layers
    python make_models_layers.py --out <目录>

生成后用**已有镜像当构建上下文**来构建，这样不用把 52GB 的文件重新喂给构建器
（实测 1.7 秒就能建一层，而不是先花 58 分钟传上下文）：

    docker buildx build \
      --build-context src=docker-image://local-multimodal-models:latest \
      --load -t local-multimodal-models:v2 <上一步的 --out 目录>
"""
import argparse
import io
import os
import sys

ROOT_DEFAULT = r"E:/本地多模态助手-Docker"
IMG_PREFIX = "/models"          # 镜像内路径
GROUP_MB = 64                   # 小于这个体积的文件，按"所在目录"合并成一层


def walk_files(models_dir: str):
    out = []
    for dirpath, _dirs, files in os.walk(models_dir):
        for fn in sorted(files):
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, models_dir).replace("\\", "/")
            out.append((os.path.getsize(fp), rel))
    return sorted(out)


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT_DEFAULT, help="交付包根目录（含 models/）")
    ap.add_argument("--out", default=os.path.join(os.environ.get("TEMP", "/tmp"),
                                                  "mm-models-layers"))
    ap.add_argument("--group-mb", type=int, default=GROUP_MB)
    args = ap.parse_args()

    models_dir = os.path.join(args.root.replace("/", os.sep), "models")
    if not os.path.isdir(models_dir):
        print("找不到 models 目录：%s" % models_dir)
        return 1

    files = walk_files(models_dir)
    total = sum(s for s, _ in files)
    thr = args.group_mb * 1024 * 1024

    big = [(s, r) for s, r in files if s >= thr]
    small = [(s, r) for s, r in files if s < thr]

    # ⚠️⚠️ 小文件"按目录合并"有一个必须避开的坑（2026-09-27 实测踩到）：
    #    `COPY src/dir/. dst/` 复制的是**整个目录** —— 若该目录里还躺着大文件
    #    （`ollama/blobs/` 就是：3 个大 blob 与 1 个小文件同目录），
    #    会把那 38GB **再复制一遍**，镜像直接从 53GB 涨到 91GB。
    #    ⇒ 规则：该目录里出现过大文件 → 小文件**逐个成层**；否则才整目录合并。
    big_dirs = {os.path.dirname(r) or "." for _s, r in big}
    groups, per_file_small = {}, []
    for s, r in small:
        d = os.path.dirname(r) or "."
        if d in big_dirs:
            per_file_small.append((s, r))
        else:
            groups.setdefault(d, []).append(r)

    lines = [
        "# ⚠️ 本文件由 make_models_layers.py 生成，不要手改。",
        "# 为什么不是一个 COPY models /models：见脚本头部注释（单 50.9GB 层推不上去）。",
        "FROM scratch",
    ]
    layers = []
    for s, r in big:
        lines.append('COPY --from=src %s/%s %s/%s' % (IMG_PREFIX, r, IMG_PREFIX, r))
        layers.append((s, [r]))
    for s, r in per_file_small:
        lines.append('COPY --from=src %s/%s %s/%s' % (IMG_PREFIX, r, IMG_PREFIX, r))
        layers.append((s, [r]))
    for d, rs in sorted(groups.items()):
        src = "%s/%s/. " % (IMG_PREFIX, d) if d != "." else "%s/. " % IMG_PREFIX
        dst = "%s/%s/" % (IMG_PREFIX, d)
        lines.append("COPY --from=src %s%s" % (src, dst))
        layers.append((sum(os.path.getsize(os.path.join(models_dir, x.replace("/", os.sep)))
                           for x in rs), rs))

    # ★ 自检：各层体积合计必须**等于**文件总量。
    #   不等于就说明有文件被复制了两遍（或漏了）—— 上面那个坑正是这么发现的。
    layer_total = sum(s for s, _ in layers)
    if abs(layer_total - total) > 1e6:
        print("❌ 自检失败：各层合计 %.2f GB ≠ 文件总量 %.2f GB —— 有文件被重复复制了"
              % (layer_total / 1e9, total / 1e9))
        return 1

    os.makedirs(args.out, exist_ok=True)
    df_path = os.path.join(args.out, "Dockerfile")
    with open(df_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    layers.sort(reverse=True)
    print("模型目录：%s" % models_dir)
    print("文件 %d 个，合计 %.2f GB（十进制）" % (len(files), total / 1e9))
    print("层数：%d（大文件各自一层 %d 个；同目录有大文件的小文件逐个成层 %d 个；"
          "其余按目录合并 %d 层）"
          % (len(layers), len(big), len(per_file_small), len(groups)))
    print("自检：各层合计 %.2f GB == 文件总量 ✅" % (layer_total / 1e9))
    print("最大的 5 层：")
    for s, rs in layers[:5]:
        print("   %7.2f GB   %s%s" % (s / 1e9, rs[0], " 等 %d 个文件" % len(rs) if len(rs) > 1 else ""))
    print("\n已写出：%s" % df_path)
    print("\n构建命令：")
    print("  docker buildx build --build-context src=docker-image://local-multimodal-models:latest \\")
    print("    --load -t local-multimodal-models:v2 \"%s\"" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
