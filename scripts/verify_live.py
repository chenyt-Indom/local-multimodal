# -*- coding: utf-8 -*-
"""对着**正在运行的后端**（默认 http://127.0.0.1:8000）实测三件事：

  A. 环境与模型状态（含"到底有多少在显存里" —— 用 ollama /api/ps 的 size_vram）
  B. **多轮衔接**：连着几轮对话，量"第一段输出出现"的耗时，确认模型没被卸载/重载
  C. **文生图**：让模型真的画一张，解码 PNG 检查尺寸与内容（不是纯色/空白）
  D. **画完图马上续话**：验证"画图前把对话模型请出显存、画完自动请回来"这条链路

为什么要单写一个"打接口"的脚本：仓库里其余 test_*.py 大多是**在进程内**
import backend 之后做逻辑断言，量不到"用户真实感受到的等待"。
这个脚本量的是**端到端**：HTTP 出去 → 后端 → Ollama → 第一个事件回来。

跑法（必须用 Python 3.14，与后端一致）：
    python scripts/verify_live.py
    python scripts/verify_live.py --skip-image      # 只验多轮衔接，不画图（省显存）
"""
import argparse
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ⚠️ 加 line_buffering：不加的话重定向到文件时会**整块缓存**，跑十分钟看不到一行，
#    很容易被误判成"卡住了"（2026-09-27 实测踩到）。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

BASE = "http://127.0.0.1:8000"
OLLAMA = "http://127.0.0.1:11434"
SESSION = "verify-live-%d" % int(time.time())

PASS, FAIL = 0, []

# ── 时间线采样 ────────────────────────────────────────────────────────────
# 为什么要它：/api/ps 说"模型还在显存里"，但用户体感是"等了两分钟" ——
# 只有把「显卡实际占用」和「Ollama 自报的显存占用」**同时**按秒采下来，
# 才能看出到底是"没卸载"还是"权重被挤出去了"。
TRACE = []
_TRACE_T0 = time.time()
_TRACE_ON = False


def mark(label):
    if _TRACE_ON:
        TRACE.append((round(time.time() - _TRACE_T0, 1), "MARK", label))


def gpu_used_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8).stdout.strip().splitlines()
        return int(out[0].strip()) if out else -1
    except Exception:
        return -1


def _sampler():
    while True:
        ps = ps_models()
        tot = sum(ps.values())
        names = ",".join(ps) or "-"
        TRACE.append((round(time.time() - _TRACE_T0, 1), "SAMPLE",
                      gpu_used_mib(), tot, names))
        time.sleep(2)


def start_trace():
    global _TRACE_ON
    _TRACE_ON = True
    threading.Thread(target=_sampler, daemon=True, name="trace").start()


def dump_trace():
    if not TRACE:
        return
    print()
    print("=" * 70)
    print("显存时间线（t=秒 | 显卡实际占用 | Ollama 自报显存 | 已加载模型）")
    print("=" * 70)
    last = None
    for row in TRACE:
        if row[1] == "MARK":
            print("  %7.1fs  ★ %s" % (row[0], row[2]))
        else:
            cur = (row[2], row[3] // (1 << 20), row[4])
            # 只在"有变化"时打印，避免刷屏；但每 30 秒强制打一行
            if last is None or abs(cur[0] - last[0]) >= 200 or cur[2] != last[2] \
                    or int(row[0]) % 30 == 0:
                print("  %7.1fs     %5d MiB  |  %5d MiB  | %s"
                      % (row[0], row[2], row[3], row[4]))
                last = cur


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name + (("   " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  [!!] " + name + (("   → " + str(detail)) if detail else ""))


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def ps_models():
    """Ollama 当前加载的模型 → {name: size_vram}"""
    try:
        d = http_get(OLLAMA + "/api/ps", timeout=5)
        return {str(m.get("name")): int(m.get("size_vram") or 0)
                for m in (d.get("models") or [])}
    except Exception:
        return {}


def chat(messages, model=None, stop_after_first=False, max_wait=600):
    """POST /api/chat 并读 NDJSON 流。

    返回 dict：first_kind/first_t（第一个实质事件的类型与耗时）、
    status_msg（后端推的"正在加载模型"提示）、image（解码后的 PNG bytes）、
    content、elapsed、err
    """
    body = {"messages": messages, "stream": True, "session_id": SESSION}
    if model:
        body["model"] = model
    req = urllib.request.Request(BASE + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    out = {"first_kind": None, "first_t": None, "status_msg": None,
           "image": None, "content": "", "elapsed": None, "err": None,
           "img_origin": None, "kinds": [], "notes": [], "tools": [],
           "t_tool": None, "t_image": None}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=max_wait) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # 记下事件类型序列，出问题时能一眼看出"卡在哪一步"
                _tn = round(time.time() - t0, 1)
                if "ui" in obj:
                    _u = obj["ui"] or {}
                    out["kinds"].append("ui:" + str(_u.get("type")))
                    if _u.get("type") == "image":
                        out["t_image"] = _tn
                elif "tool_start" in obj:
                    out["kinds"].append("tool_start")
                    out["tools"].append(str((obj["tool_start"] or {}).get("name")))
                    out["t_tool"] = _tn
                elif "note" in obj:
                    out["kinds"].append("note")
                    out["notes"].append(str(obj["note"])[:120])
                elif obj.get("message", {}).get("thinking"):
                    out["kinds"].append("thinking")
                elif obj.get("message", {}).get("content"):
                    out["kinds"].append("content")
                elif obj.get("status"):
                    out["kinds"].append("status")
                if obj.get("status") and not out["status_msg"]:
                    out["status_msg"] = obj["status"]
                if obj.get("error"):
                    out["err"] = str(obj["error"])[:200]
                    break
                # 图片事件
                ui = obj.get("ui") or {}
                if ui.get("type") == "image" and ui.get("b64"):
                    try:
                        out["image"] = base64.b64decode(ui["b64"])
                        out["img_origin"] = ui.get("origin")
                    except Exception:
                        pass
                # 第一个"实质输出"：思考 / 正文 / 工具调用 都算（用户看到东西了）
                if out["first_t"] is None:
                    msg = obj.get("message") or {}
                    if msg.get("thinking") or msg.get("content") or obj.get("tool_start"):
                        out["first_t"] = time.time() - t0
                        out["first_kind"] = ("thinking" if msg.get("thinking")
                                             else "content" if msg.get("content")
                                             else "tool_start")
                m = obj.get("message") or {}
                if isinstance(m.get("content"), str):
                    out["content"] += m["content"]
                if stop_after_first and out["first_t"] is not None:
                    break
    except urllib.error.HTTPError as e:
        out["err"] = "HTTP %s: %s" % (e.code, (e.read() or b"")[:160])
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, e)
    out["elapsed"] = time.time() - t0
    return out


def png_info(b: bytes):
    """不依赖 PIL 的轻量检查：能否解析尺寸 + 是否"基本纯色"（可能是空白图）。"""
    from PIL import Image
    im = Image.open(io.BytesIO(b))
    im.load()
    rgb = im.convert("RGB")
    small = rgb.resize((64, 64))
    px = list(small.getdata())
    uniq = len({(r // 16, g // 16, b // 16) for r, g, b in px})
    # 标准差：纯色图接近 0
    n = len(px)
    mean = [sum(p[i] for p in px) / n for i in range(3)]
    var = sum(sum((p[i] - mean[i]) ** 2 for i in range(3)) for p in px) / (n * 3)
    return {"size": im.size, "mode": im.mode, "uniq_bins": uniq,
            "std": var ** 0.5, "bytes": len(b)}


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-image", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--trace", action="store_true",
                    help="按秒采样显卡/显存占用，最后打印时间线")
    ap.add_argument("--save-dir", default="",
                    help="把收到的图片存到这个目录（便于人工看一眼）")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    if args.trace:
        start_trace()
    mark("开跑")

    print("=" * 70)
    print("A. 环境与模型状态")
    print("=" * 70)
    try:
        cfg = (http_get(BASE + "/api/config") or {}).get("config") or {}
        dm = cfg.get("default_model")
        cm = cfg.get("code_model")
        keep = cfg.get("model_keep_alive")
        print("  后端配置：default_model=%s  code_model=%s  model_keep_alive=%s  num_ctx=%s"
              % (dm, cm, keep, cfg.get("num_ctx")))
        check("后端在跑且能读到配置", bool(dm))
        check("保活时长够长（>=1h，避免聊一半被卸载）",
              str(keep).endswith("h") or str(keep) == "-1", keep)
    except Exception as e:
        check("后端在跑且能读到配置", False, e)
        return 1

    loaded0 = ps_models()
    print("  Ollama 已加载：%s" % (loaded0 or "（无）"))
    for n, v in loaded0.items():
        print("     %-24s 显存中 %5.2f GB" % (n, v / 1073741824.0))

    print()
    print("=" * 70)
    print("B. 多轮衔接：连着问，量「第一段输出」的耗时（关键）")
    print("=" * 70)
    h = [{"role": "user", "content": "1+1 等于几？只回一个数字。"}]
    mark("B 第1轮 发出")
    r1 = chat(h, model=args.model or None, stop_after_first=True)
    mark("B 第1轮 首段 %.2fs" % (r1["first_t"] or -1))
    print("  第 1 轮  首段 %s  用时 %.2fs  总 %.2fs  err=%s"
          % (r1["first_kind"], r1["first_t"] or -1, r1["elapsed"], r1["err"]))
    check("第 1 轮拿到了输出", bool(r1["first_t"]), r1["err"] or "")

    h += [{"role": "assistant", "content": "2"},
          {"role": "user", "content": "那 2+2 呢？同样只回数字。"}]
    r2 = chat(h, model=args.model or None, stop_after_first=True)
    print("  第 2 轮  首段 %s  用时 %.2fs  总 %.2fs"
          % (r2["first_kind"], r2["first_t"] or -1, r2["elapsed"]))
    check("第 2 轮拿到了输出", bool(r2["first_t"]))

    # 故意空 30 秒（模拟"人看着屏幕想事情"），模型不该被卸载
    print("  … 故意等 30 秒（模拟用户思考时间）")
    time.sleep(30)
    loaded_mid = ps_models()

    h += [{"role": "assistant", "content": "4"},
          {"role": "user", "content": "3+3 呢？"}]
    r3 = chat(h, model=args.model or None, stop_after_first=True)
    print("  第 3 轮  首段 %s  用时 %.2fs  总 %.2fs"
          % (r3["first_kind"], r3["first_t"] or -1, r3["elapsed"]))
    check("第 3 轮拿到了输出", bool(r3["first_t"]))

    loaded1 = ps_models()
    key = args.model or dm
    print("  /api/ps 报告（⚠️ 这个字段实测会报旧数，只作参考，判据看下面的耗时）：%s"
          % (list(loaded_mid) or "（无）"))
    # ⚠️ 判据用**耗时**而不是 /api/ps：一次重载至少 20 秒（实测 21.7s），
    #    所以"首段 < 5 秒"就等价于"没有重载"，比读一个会说谎的字段可靠。
    if r2["first_t"] is not None:
        check("第 2 轮没有重载（首段 < 5 秒）", r2["first_t"] < 5.0,
              "%.2fs" % r2["first_t"])
    if r3["first_t"] is not None:
        check("空档 %d 秒后再问，仍没有重载（首段 < 5 秒）" % 30, r3["first_t"] < 5.0,
              "%.2fs" % r3["first_t"])
    if r1["first_t"] and r2["first_t"]:
        print("  对比：第 1 轮 %.2fs → 第 2 轮 %.2fs" % (r1["first_t"], r2["first_t"]))

    if args.skip_image:
        return summary()

    print()
    print("=" * 70)
    print("C. 文生图：让模型真的画一张")
    print("=" * 70)
    print("  Ollama：%s" % (ps_models() or "（无）"))
    # ⚠️ 配置里 ask_mode=deep 时，模型会**先反问澄清**再动手 —— 这里要画图，
    #    所以明确说"不要提问、直接画"，否则量到的是"它在问问题"而不是画图。
    print("  → 发：直接生成图片，不要提问 —— 一只橘猫坐在窗台上，写实风格")
    h2 = [{"role": "user",
           "content": "直接生成图片，不要提问、不要与我确认：一只橘猫坐在窗台上，写实风格。"
                      "请立刻调用画图工具生成。"}]
    mark("C 画图请求 发出")
    rimg = chat(h2, model=args.model or None, max_wait=600)
    mark("C 收到图片 总耗时 %.1fs" % rimg["elapsed"])
    if rimg["image"]:
        info = png_info(rimg["image"])
        print("  收到图片：%d×%d %s  %.2f MB  色彩分档 %d  像素标准差 %.1f  origin=%s"
              % (info["size"][0], info["size"][1], info["mode"],
                 info["bytes"] / 1048576.0, info["uniq_bins"], info["std"],
                 rimg["img_origin"]))
        print("  计时：模型决定调工具 %.1fs → 图片出来 %.1fs（**生图本身约 %.1fs**）"
              " → 整轮 %.1fs"
              % (rimg["t_tool"] or -1, rimg["t_image"] or -1,
                 ((rimg["t_image"] or 0) - (rimg["t_tool"] or 0)) or -1,
                 rimg["elapsed"]))
        print("  工具调用：%s" % (rimg["tools"] or "（无）"))
        if args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
            fp = os.path.join(args.save_dir,
                              "verify_%s.png" % time.strftime("%H%M%S"))
            with open(fp, "wb") as f:
                f.write(rimg["image"])
            print("  已存图：%s" % fp)
        check("文生图返回了 PNG", info["size"][0] >= 512 and info["size"][1] >= 512,
              "%s" % (info["size"],))
        check("图片不是纯色/空白（有真实内容）",
              info["uniq_bins"] >= 20 and info["std"] > 10,
              "uniq=%d std=%.1f" % (info["uniq_bins"], info["std"]))
    else:
        check("文生图返回了 PNG", False, rimg["err"] or "没收到图片事件")
        print("  事件序列：%s" % (" → ".join(rimg["kinds"][:25]) or "（空）"))
        print("  调用的工具：%s" % (rimg["tools"] or "（没调任何工具）"))
        for n in rimg["notes"][:6]:
            print("  后端提示：%s" % n)
        print("  模型正文（截断）：%s" % (rimg["content"] or "（空）")[:300])
    print("  画图后 Ollama：%s" % (ps_models() or "（无 → 按设计被请出显存了）"))

    print()
    print("=" * 70)
    print("D. 画完马上续话：模型能不能自动「请回来」")
    print("=" * 70)
    h2 += [{"role": "assistant", "content": "（已生成图片）"},
           {"role": "user", "content": "刚才那张图里是什么动物？一句话。"}]
    rd = chat(h2, model=args.model or None, stop_after_first=True, max_wait=600)
    mark("D 画完立刻问 首段 %.2fs" % (rd["first_t"] or -1))
    print("  画完立刻问  首段 %s  用时 %.2fs" % (rd["first_kind"], rd["first_t"] or -1))
    if rd["status_msg"]:
        print("  后端提示：%s" % rd["status_msg"])
    print("  问完 Ollama：%s" % (ps_models() or "（无）"))

    print("  … 等 25 秒，让后台预热（rewarm_async）跑完")
    time.sleep(25)
    loaded_after = ps_models()
    print("  /api/ps 报告（仅参考）：%s" % (loaded_after or "（无）"))

    h2 += [{"role": "assistant", "content": "猫"},
           {"role": "user", "content": "那它是什么颜色？一句话。"}]
    rd2 = chat(h2, model=args.model or None, stop_after_first=True)
    print("  再来一条    首段 %s  用时 %.2fs" % (rd2["first_kind"], rd2["first_t"] or -1))
    mark("D 再来一条 首段 %.2fs" % (rd2["first_t"] or -1))
    check("画完图后能快速衔接（首段 < 5 秒 = 没重载）", (rd2["first_t"] or 99) < 5.0,
          "%.2fs" % (rd2["first_t"] or -1))
    if (rd["first_t"] or 0) > 10:
        print("  ⚠️ 注意：画完图后**第一条**消息等了 %.1fs —— 那是因为画图时"
              "对话模型被请出了显存，这条触发了重新加载。" % rd["first_t"])
        if not rd["status_msg"]:
            print("     而且后端**没有**给出「正在加载模型」的提示（它判断模型仍在，"
                  "但实际不在）—— 这是要修的点。")

    return summary(with_trace=True)


def summary(with_trace: bool = False) -> int:
    if with_trace:
        dump_trace()
    print()
    print("=" * 70)
    print("结果：通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
    for f in FAIL:
        print("  [!!] %s" % f)
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
