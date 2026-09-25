# -*- coding: utf-8 -*-
"""把仓库里所有 test_*.py 依次跑一遍，汇总通过/失败。（没有统一运行器，补一个）

用法：python run_all_tests.py [--only 关键字]
"""
import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 420          # 单个测试最多 7 分钟（图片生成类偏慢）

only = ""
if "--only" in sys.argv:
    only = sys.argv[sys.argv.index("--only") + 1]

files = sorted(f for f in os.listdir(ROOT)
               if f.startswith("test_") and f.endswith(".py"))
if only:
    files = [f for f in files if only in f]

print("=" * 70)
print("跑 %d 个测试文件（逐个串行，单个上限 %d 秒）" % (len(files), TIMEOUT))
print("=" * 70)

results = []
for i, f in enumerate(files, 1):
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, f], cwd=ROOT,
                           stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=TIMEOUT)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        code = p.returncode
        status = "PASS" if code == 0 else "FAIL"
    except subprocess.TimeoutExpired:
        out, err, code, status = "", "超时", -1, "TIMEOUT"
    dt = time.time() - t0

    # 抓"通过 N 项，失败 M 项"这类汇总行，没有就取最后一行非空
    tail = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip()]
    summary = ""
    for line in reversed(tail):
        if "通过" in line and "失败" in line:
            summary = line
            break
    if not summary and tail:
        summary = tail[-1][:80]

    results.append((f, status, dt, summary, code))
    print("\n[%2d/%2d] %-34s %-8s %5.1fs  %s"
          % (i, len(files), f, status, dt, summary))
    if status != "PASS":
        print("        最后几行输出：")
        for line in tail[-6:]:
            print("          " + line[:110])

print("\n" + "=" * 70)
print("汇总")
print("=" * 70)
ok = [r for r in results if r[1] == "PASS"]
bad = [r for r in results if r[1] != "PASS"]
for f, s, dt, summ, code in bad:
    print("  %-8s %-34s %s" % (s, f, summ[:70]))
print("\n  通过 %d / %d ，未通过 %d" % (len(ok), len(results), len(bad)))
sys.exit(1 if bad else 0)
