# -*- coding: utf-8 -*-
"""生成「安装包清单.json」—— 交付包落地自检用的账本。

**为什么要它**：20GB+ 的包最常见的翻车方式是**静默漏文件**（U 盘拔早了、拷贝中断），
而且往往到部署那一刻才报错，很难定位。有了清单 + 校验脚本，落地时双击一下
就能知道"少了哪个文件、哪个 tar 不完整"。

**记什么**（按"能不能查出问题"来选，不是越全越好）：
  · 关键大文件（3 个镜像 tar + Docker 安装程序）→ **sha256 + 字节数**（拷坏了能查出来）
  · 模型目录 → **文件数 + 总字节 + 每个子文件的相对路径与大小**
    （模型动辄 15GB，逐个算哈希要好几分钟；文件数+大小足以发现漏拷与截断）
  · 必需的小文件 → 只记字节数（秒级跑完）

用法（在包根目录跑，需要 Python；没有 Python 也没关系，交付时由打包方生成）：
    python make_manifest.py

⚠️ 生成后**必须**跟着跑一遍 `校验安装包.bat`，确认自检能全过 ——
   清单本身写错了（漏了某个必需文件），校验脚本就形同虚设。
"""
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

# 关键大文件：记 sha256（这几个文件坏了，包就没用了）
KEY_FILES = [
    "images/local-multimodal-app.tar",
    "images/local-multimodal-app-gpu.tar",
    "images/local-multimodal-ollama.tar",
    "installers/DockerDesktopInstaller.exe",
]
# 必需的小文件：只记字节数
REQUIRED_FILES = [
    "compose.yml",
    "compose.gpu.yml",
    "一键部署.bat",
    "查看状态.bat",
    "停止运行.bat",
    "完全卸载.bat",
    "open-folder-agent.ps1",
    "使用说明.md",
    "校验安装包.bat",
    "校验安装包.ps1",
    "data/config.json",
]
# 模型目录：记文件清单 + 大小
DIRS = [
    "models/ollama",
    "models/sd-turbo",
    "models/esrgan",
]


def sha256_of(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    man = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": os.path.basename(ROOT),
        "key_files": [],
        "required_files": [],
        "dirs": [],
    }
    missing = []
    for rel in KEY_FILES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            missing.append(rel)
            print("  [!!] 缺文件，跳过：%s" % rel)
            continue
        n = os.path.getsize(p)
        print("  算 sha256：%-42s %8.1f MB …" % (rel, n / 1048576.0))
        man["key_files"].append({"path": rel, "bytes": n, "sha256": sha256_of(p)})
    for rel in REQUIRED_FILES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            missing.append(rel)
            print("  [!!] 缺文件，跳过：%s" % rel)
            continue
        man["required_files"].append({"path": rel, "bytes": os.path.getsize(p)})
    for rel in DIRS:
        d = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isdir(d):
            missing.append(rel + "/")
            print("  [!!] 缺目录，跳过：%s" % rel)
            continue
        lst, total = [], 0
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                lst.append([os.path.relpath(fp, d).replace(os.sep, "/"), sz])
                total += sz
        lst.sort()
        man["dirs"].append({"path": rel, "files": len(lst), "bytes": total, "list": lst})
        print("  目录 %-18s %4d 个文件  %10.1f MB" % (rel, len(lst), total / 1048576.0))

    out = os.path.join(ROOT, "安装包清单.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print("\n已写出：%s" % out)
    if missing:
        # ⚠️ 别静默："清单生成成功"但漏了东西，等于给校验脚本开了一个洞。
        print("⚠️ 有 %d 项没找到（清单里不会有它们，校验也查不出来）：" % len(missing))
        for m in missing:
            print("    · %s" % m)
        return 1
    print("共 %d 个关键文件 + %d 个必需文件 + %d 个模型目录"
          % (len(man["key_files"]), len(man["required_files"]), len(man["dirs"])))
    print("接着跑一遍「校验安装包.bat」确认自检能全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
