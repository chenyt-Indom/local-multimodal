# -*- coding: utf-8 -*-
r"""把「本地多模态助手」组装成一个**自包含便携包**（然后可打成单文件自解压安装器）。

目标布局（包根默认 E:\本地多模态助手）：
    <包根>\
      启动窗口.pyw            桌面窗口启动器（会自动拉起包内 Ollama）
      启动.bat                双击入口（GBK+CRLF）
      使用说明.md
      app\                    代码（run.py / backend\ / frontend\ …）
      runtime\python\         Python 运行时（含 fastapi/torch/diffusers 等依赖）
      ollama\                 ollama.exe + lib\（**自带**，不依赖系统托盘版）
      models\
        ollama\               36GB —— OLLAMA_MODELS 指这里
        sd\sdxl-base\         13GB
        sd\sdxl-inpaint\
        rembg\  upscale\  asr\
      data\                   记忆 / 知识库 / 文库 / config.json

为什么要有这个脚本（2026-10-16）：之前权重散落在 D:\、E:\ 的多个绝对路径上，
包一挪窝就找不到 → 代码回退去联网下载 → 用户看到"一直等待上线中"。
组装成"包内相对路径"后，整块拷到任何盘/任何机器都能直接用。

⚠️ 本脚本**只复制、不删除**。清理旧副本是另一件事，等新包验证通过再说。

用法：python scripts\build_portable_bundle.py [目标目录]
"""
import io
import os
import shutil
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SRC = r"D:\local-multimodal-src"
DIST = r"D:\local-multimodal-dist"
PY_SRC = r"C:\Users\19853\AppData\Local\Programs\Python\Python314"
OLLAMA_SRC = r"C:\Users\19853\AppData\Local\Programs\Ollama"

BUNDLE = sys.argv[1] if len(sys.argv) > 1 else r"E:\本地多模态助手"

# 这些是"开发产物/运行数据"，不该进包里
CODE_EXCLUDE_DIRS = {
    ".git", "build", "build-lite", "dist-lite", "_qa_out", "_qa_docs",
    "__pycache__", "releases", "saved_images", "image_library", "sessions",
    "logs", "data", "rembg", "test_config.json", ".venv", "env",
}
CODE_EXCLUDE_FILES = {
    ".DS_Store",
    # ⚠️ code-server 的源码压缩包 —— 解出来的目录才是运行时真正要用的
    #    （`workspace.find_code_server()` 找的是 `vendor/code-server-*/bin/`），
    #    这个 tar.gz 只是多余的一份，占 206MB。
    os.path.join("vendor", "code-server-windows.tar.gz"),
}
CODE_EXCLUDE_SUFFIX = {".pyc", ".pyo", ".bak", ".log"}

# (源路径, 包内相对路径)  —— 全部是"从别处收拢进来"的模型资产
MODEL_ASSETS = [
    (r"D:\local-multimodal-src\rembg",            r"models\rembg"),
    (r"D:\local-multimodal-models\sherpa-asr",    r"models\asr"),
    (r"D:\training_data\RealESRGAN_x4plus.pth",   r"models\upscale\RealESRGAN_x4plus.pth"),
    (r"E:\local-multimodal-models\sdxl-base",     r"models\sd\sdxl-base"),
    (r"E:\local-multimodal-models\sdxl-inpaint",  r"models\sd\sdxl-inpaint"),
    (r"E:\ollama-models",                         r"models\ollama"),
]

DIRS = ["app", r"runtime\python", "ollama", r"models\ollama", r"models\sd",
        r"models\rembg", r"models\upscale", r"models\asr", "data", "_build"]


def log(msg=""):
    print(msg, flush=True)


def dir_size(p):
    if not os.path.exists(p):
        return 0
    if os.path.isfile(p):
        return os.path.getsize(p)
    t = 0
    for root, _d, files in os.walk(p):
        for f in files:
            try:
                t += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return t


def gb(n):
    return "%.2f GB" % (n / 1e9)


def robocopy(src, dst, extra=None):
    """用 robocopy 复制（大目录快得多，且支持排除）。返回退出码（<8 即成功）。"""
    cmd = ["robocopy", src, dst, "/E", "/MT:16", "/NFL", "/NDL", "/NDL",
           "/NJH", "/NJS", "/R:1", "/W:1"]
    if extra:
        cmd += extra
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode


def step(n, total, title):
    # 只打印序号（不打 "/总数"）：模型资产是变长的，写死总数只会误导。
    log()
    log("[%d] %s" % (n, title))


def main():
    total = 7
    log("=" * 70)
    log("组装便携整合包 -> %s" % BUNDLE)
    log("=" * 70)
    if os.path.exists(os.path.join(BUNDLE, "app", "run.py")):
        log("⚠️ 目标目录看起来已经组装过了；本次会**覆盖同名文件**（不删除多余文件）")

    t_all = time.time()

    # 0) 建目录
    step(0, total, "建目录骨架")
    for d in DIRS:
        os.makedirs(os.path.join(BUNDLE, d), exist_ok=True)
    log("    %s" % ", ".join(DIRS))

    # 1) 代码
    step(1, total, "复制应用代码 -> app\\")
    ex = ["/XD"] + [os.path.join(SRC, d) for d in sorted(CODE_EXCLUDE_DIRS)]
    ex += ["/XF"] + sorted(CODE_EXCLUDE_FILES)
    for suf in sorted(CODE_EXCLUDE_SUFFIX):
        ex += ["*.%s" % suf.lstrip(".")]
    rc = robocopy(SRC, os.path.join(BUNDLE, "app"), ex)
    ok = rc is not None and rc < 8
    log("    rc=%s %s   体积=%s" % (rc, "OK" if ok else "✗ 失败",
                                    gb(dir_size(os.path.join(BUNDLE, "app")))))

    # 2) Python 运行时
    step(2, total, "复制 Python 运行时 -> runtime\\python\\")
    rc = robocopy(PY_SRC, os.path.join(BUNDLE, "runtime", "python"),
                  ["/XD", os.path.join(PY_SRC, "Doc")])
    log("    rc=%s   体积=%s" % (rc, gb(dir_size(os.path.join(BUNDLE, "runtime")))))
    for f in ("python.exe", "pythonw.exe"):
        p = os.path.join(BUNDLE, "runtime", "python", f)
        log("    %s 存在=%s" % (f, os.path.isfile(p)))

    # 3) Ollama（自带）
    step(3, total, "复制自带 Ollama -> ollama\\")
    for name in ("ollama.exe", "app.ico"):
        s = os.path.join(OLLAMA_SRC, name)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(BUNDLE, "ollama", name))
    rc = robocopy(os.path.join(OLLAMA_SRC, "lib"), os.path.join(BUNDLE, "ollama", "lib"))
    log("    rc=%s   体积=%s" % (rc, gb(dir_size(os.path.join(BUNDLE, "ollama")))))

    # 4~6) 模型资产
    n = 4
    for src, rel in MODEL_ASSETS:
        step(n, total + len(MODEL_ASSETS) - 3, "收拢模型 %s -> %s" % (src, rel))
        n += 1
        if not os.path.exists(src):
            log("    ⚠️ 源不存在，跳过")
            continue
        dst = os.path.join(BUNDLE, rel)
        t0 = time.time()
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        else:
            os.makedirs(os.path.dirname(dst.rstrip("\\")), exist_ok=True)
            rc = robocopy(src, dst)
            log("    rc=%s" % rc)
        log("    完成，用时 %.1f 秒   体积=%s" % (time.time() - t0, gb(dir_size(dst))))

    # 7) 启动器 / 配置 / 说明
    step(7, total, "放启动器、配置与说明")
    # ⚠️ 启动器优先取**仓库里纳管的**那份（`launcher\启动窗口.pyw`）——
    #    2026-10-16 才发现它原来只散落在 D:\local-multimodal-dist 里、**没进 git**，
    #    改一次就丢一次。dist 那份留作回退。
    launcher_src = os.path.join(SRC, "launcher", "启动窗口.pyw")
    if not os.path.isfile(launcher_src):
        launcher_src = os.path.join(DIST, "启动窗口.pyw")
    shutil.copy2(launcher_src, os.path.join(BUNDLE, "启动窗口.pyw"))
    log("    启动器来源: %s" % launcher_src)
    cfg_src = os.path.join(SRC, "config.json")
    if os.path.isfile(cfg_src):
        shutil.copy2(cfg_src, os.path.join(BUNDLE, "data", "config.json"))
    log("    启动窗口.pyw + data\\config.json")

    log()
    log("=" * 70)
    log("组装完成，用时 %.1f 秒" % (time.time() - t_all))
    log("包总大小（不含 _build）：")
    for d in sorted(os.listdir(BUNDLE)):
        p = os.path.join(BUNDLE, d)
        if os.path.isdir(p):
            log("   %-16s %s" % (d, gb(dir_size(p))))
    log("=" * 70)


if __name__ == "__main__":
    main()
