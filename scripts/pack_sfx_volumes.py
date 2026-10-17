#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把便携整合包打成「分卷自解压安装器」。

━━ 为什么必须分卷（2026-10-17 实测，血泪教训）━━
Windows **无法加载任何大于 4GB 的 exe**：
  · 3GB 的自解压包 → 能启动；
  · 5GB 的自解压包 → CreateProcess 直接返回 193
    「不是有效的 Win32 应用程序 / not a valid Win32 application」；
  · 64 位 PE 也一样（拿 7z.exe 追加 5GB 尾巴复测，同样 193）。
⇒ 66GB 的单文件 exe 在 Windows 上**物理上不可能存在**。
   `7z a -sfx7z.sfx 大包.exe` 会生成一个永远打不开的废文件（`7z l` 却能正常列出，
   极具欺骗性 —— 一定要真的启动一次才知道）。

正确做法：`-v<卷大小>` 分卷。7z 会产出
    本地多模态助手_一键安装.exe            （仅 215KB 的引导壳）
    本地多模态助手_一键安装.7z.001 ... .0NN （数据卷）
用户仍然**只需双击那个 exe**（体验没变），但 13 个文件必须放在同一个文件夹里。

其它两个实测要点：
  · 卷大小 6GB 可用（8GB 数据实测 10 秒完整展开）；只要求 **exe 本身** < 4GB。
  · `7z.sfx` 支持命令行静默展开：`exe -o<目标目录> -y`，
    可以直接用来做自动化验收（不用点弹窗）。

用法:
    python scripts/pack_sfx_volumes.py --out "E:\\本地多模态助手-安装包"
    python scripts/pack_sfx_volumes.py --out <目录> --bundle <整合包目录> --vol 6000
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import time

SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]
MAX_EXE_BYTES = 4 * 1024 ** 3          # 硬限制：exe 超过这个大小就加载不了
SFX_BASENAME = "本地多模态助手_一键安装"


def find_7z() -> str:
    for p in SEVENZIP_CANDIDATES:
        if os.path.isfile(p):
            return p
    w = shutil.which("7z")
    if w:
        return w
    raise SystemExit("找不到 7z.exe，请先安装 7-Zip")


def tree_stat(root: str):
    n = sz = 0
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            n += 1
            try:
                sz += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return n, sz


def sfx_module(sevenz: str) -> str:
    d = os.path.join(os.path.dirname(sevenz), "7z.sfx")
    if not os.path.isfile(d):
        raise SystemExit("找不到 7z.sfx 模块")
    with open(d, "rb") as f:
        f.seek(0x3C)
        off = struct.unpack("<I", f.read(4))[0]
        f.seek(off + 4)
        machine = struct.unpack("<H", f.read(2))[0]
    print("  SFX 模块: %s  机器码 0x%04x（%s）" % (
        d, machine, "32 位" if machine == 0x14C else "64 位"))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=r"E:\本地多模态助手", help="要打包的整合包目录")
    ap.add_argument("--out", required=True, help="安装包输出目录")
    ap.add_argument("--vol", type=int, default=6000, help="每卷大小（MB），默认 6000")
    args = ap.parse_args()

    bundle = os.path.abspath(args.bundle)
    out_dir = os.path.abspath(args.out)
    if not os.path.isdir(bundle):
        raise SystemExit("整合包目录不存在: %s" % bundle)
    os.makedirs(out_dir, exist_ok=True)
    sevenz = find_7z()

    n, sz = tree_stat(bundle)
    print("待打包: %s" % bundle)
    print("  文件 %d 个 / %.2f GiB" % (n, sz / 1024 ** 3))
    print("  预计分卷: %d 卷 × %d MB" % (int(sz // (args.vol * 1024 ** 2)) + 1, args.vol))
    print()
    sfx_module(sevenz)

    exe = os.path.join(out_dir, SFX_BASENAME + ".exe")
    for fn in os.listdir(out_dir):
        if fn.startswith(SFX_BASENAME):
            os.remove(os.path.join(out_dir, fn))

    cmd = [sevenz, "a", "-mx0", "-mmt=on", "-sfx7z.sfx", "-v%dm" % args.vol, exe, bundle]
    print("\n执行: %s" % " ".join(cmd))
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    print((r.stdout or "").strip().splitlines()[-4:] if r.stdout else "")
    if r.returncode not in (0, 1):
        raise SystemExit("打包失败 rc=%s\n%s" % (r.returncode, r.stderr))

    print("\n耗时 %.1f 秒" % (time.time() - t0))
    total = 0
    parts = 0
    for fn in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, fn)
        s = os.path.getsize(p)
        total += s
        if ".7z." in fn:
            parts += 1
        print("  %-46s %8.2f GiB" % (fn, s / 1024 ** 3))

    exe_size = os.path.getsize(exe)
    print("\n验收:")
    ok = True
    if exe_size > MAX_EXE_BYTES:
        print("  ❌ 引导 exe 超过 4GB（%.2f GiB）→ 双击必然报 193" % (exe_size / 1024 ** 3))
        ok = False
    else:
        print("  ✅ 引导 exe %.2f MB（远小于 4GB 上限）" % (exe_size / 1024 / 1024))
    print("  ✅ 数据卷 %d 个，合计 %.2f GiB" % (parts, total / 1024 ** 3))
    print("\n⚠️ 交付时务必提醒用户：%d 个文件要放在同一个文件夹，缺一卷就展不开。"
          % (parts + 2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
