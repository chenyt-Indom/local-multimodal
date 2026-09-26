# -*- coding: utf-8 -*-
"""从交付包根目录生成「部署套件」`_deploy-kit/`（就是 multimodal-deploy 镜像的内容）。

**为什么需要它**：交付包里的部署脚本其实有**三份**同名副本 ——
  1. 包根目录           ← 用户直接双击用的
  2. build/docker/      ← 从包内重建应用镜像时的构建上下文
  3. _deploy-kit/       ← 打成 multimodal-deploy:latest，供"从镜像仓库部署"的人取用
只改其中一两份 → 另外一份悄悄停在上一版。2026-09-27 实测就踩了：
包根改了"约 50GB / 58GB"，套件镜像里还是"18GB / 27GB"，而且
`data/config.json` 压根没进套件（用户按文档去找，找不到）。

所以**别手抄**：改完包根，跑一次本脚本，三份就一致了。

用法（在包根目录）：
    python make_deploy_kit.py
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.join(ROOT, "_deploy-kit")

# 套件里该有的东西（= 用户在空目录里 `docker run ... cp -r /deploy/. /out/` 之后看到的）
FILES = [
    "一键部署.bat",
    "查看状态.bat",
    "停止运行.bat",
    "完全卸载.bat",
    "校验安装包.bat",
    "校验安装包.ps1",
    "open-folder-agent.ps1",
    "compose.yml",
    "compose.gpu.yml",
    "使用说明.md",
    "从镜像仓库部署.md",
]
DIRS = [
    "data",          # 只放 config.json 样本
]

DOCKERFILE = "FROM scratch\nCOPY . /deploy\n"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(KIT, exist_ok=True)

    n_new = n_upd = 0
    for rel in FILES:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(KIT, rel)
        if not os.path.isfile(src):
            print("  [!!] 包根缺文件：%s" % rel)
            return 1
        same = os.path.isfile(dst) and open(src, "rb").read() == open(dst, "rb").read()
        shutil.copy2(src, dst)
        if same:
            print("  [same] %s" % rel)
        else:
            n_upd += 1
            print("  [UPD ] %s" % rel)

    for rel in DIRS:
        s = os.path.join(ROOT, rel)
        d = os.path.join(KIT, rel)
        if not os.path.isdir(s):
            print("  [!!] 包根缺目录：%s" % rel)
            return 1
        os.makedirs(d, exist_ok=True)
        for fn in os.listdir(s):
            # ⚠️ 包根的 data\ 是**用户数据**（记忆/会话/图片库），只取配置样本
            if fn != "config.json":
                continue
            shutil.copy2(os.path.join(s, fn), os.path.join(d, fn))
            n_new += 1
            print("  [UPD ] %s/%s" % (rel, fn))

    with open(os.path.join(KIT, "Dockerfile"), "w", encoding="utf-8", newline="\n") as f:
        f.write(DOCKERFILE)

    # 套件里不该出现任何缓存/备份
    junk = []
    for dirpath, dirnames, filenames in os.walk(KIT):
        for x in list(dirnames):
            if x == "__pycache__":
                dirnames.remove(x)
                junk.append(os.path.join(dirpath, x))
        for fn in filenames:
            if fn.endswith((".pyc", ".bak")) or ".bak-" in fn or ".bak_" in fn:
                junk.append(os.path.join(dirpath, fn))
    for j in junk:
        shutil.rmtree(j, ignore_errors=True) if os.path.isdir(j) else os.remove(j)
    if junk:
        print("  清掉 %d 个缓存/备份" % len(junk))

    print("\n套件已就绪：%s（更新 %d 项）" % (KIT, n_upd))
    print("接着跑：docker build -f _deploy-kit/Dockerfile -t local-multimodal-deploy:latest _deploy-kit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
