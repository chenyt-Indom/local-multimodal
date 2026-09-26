# -*- coding: utf-8 -*-
"""把下载好的 GGUF 导入 Ollama（代码模型 + 多模态主模型）。

为什么要走这条路（2026-09-26 实测）：
  `ollama pull qwen3-coder:30b` / `qwen3-vl:30b` 从官方 registry 拉，
  国内实测会**反复卡停**（速度掉到 0~1.8 KB/s，多次重试进度还会倒退）。
  改从 **ModelScope** 下 GGUF（实测 14~34 MB/s，快 8000 倍）再 `ollama create` 导入。

多模态的关键：**必须同时导入 mmproj（视觉投影层）**，
写法是 Modelfile 里写两个 FROM —— 第二个（mmproj）会被 ollama 认成 projector。
只导主模型的话，图片输入会报 500
`this model is missing data required for image input`。
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

GGUF = r"E:\ollama-models\gguf"
OLLAMA = r"C:\Users\19853\AppData\Local\Programs\Ollama\ollama.exe"

# 名称 → (主模型 gguf, mmproj gguf 或 None, 额外参数)
JOBS = [
    ("qwen3-coder:30b",
     "qwen3-coder-30b-Q4_K_M.gguf", None,
     ["temperature 0.7", "top_p 0.8", "top_k 20"]),
    ("qwen3-vl:30b",
     "qwen3-vl-30b-Q4_K_M.gguf", "qwen3-vl-30b-mmproj-F16.gguf",
     ["temperature 1", "top_k 20", "top_p 0.95"]),
]


def make_modelfile(main_gguf, mmproj, params, out_path):
    lines = ["FROM ./%s" % main_gguf]
    if mmproj:
        # ★ 第二个 FROM = 视觉投影层（多模态必需）
        lines.append("FROM ./%s" % mmproj)
    for p in params:
        lines.append("PARAMETER %s" % p)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return "\n".join(lines)


def main():
    ok, fail = 0, 0
    for name, main_gguf, mmproj, params in JOBS:
        print("=" * 60)
        print("导入 %s" % name)
        if not os.path.isfile(os.path.join(GGUF, main_gguf)):
            print("  [跳过] 主模型 GGUF 不在：%s" % main_gguf)
            fail += 1
            continue
        if mmproj and not os.path.isfile(os.path.join(GGUF, mmproj)):
            print("  [跳过] 投影层 GGUF 不在：%s" % mmproj)
            fail += 1
            continue
        mf = os.path.join(GGUF, "Modelfile.%s" % name.replace(":", "-"))
        body = make_modelfile(main_gguf, mmproj, params, mf)
        print("--- Modelfile ---")
        print(body)
        print("--- 开始导入（大模型要几分钟）---")
        r = subprocess.run([OLLAMA, "create", name, "-f", mf],
                           cwd=GGUF, capture_output=True, text=True)
        tail = (r.stdout or "")[-500:] + (r.stderr or "")[-500:]
        if r.returncode == 0:
            print("  ✓ %s 导入成功" % name)
            ok += 1
        else:
            print("  ✗ %s 导入失败（退出码 %d）" % (name, r.returncode))
            print("  " + tail.replace("\n", "\n  ")[-600:])
            fail += 1
    print()
    print("成功 %d 个，失败 %d 个" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
