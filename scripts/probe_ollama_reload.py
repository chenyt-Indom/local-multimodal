# -*- coding: utf-8 -*-
"""判断"多轮对话会不会重新加载模型" —— 用 Ollama 返回体里的权威字段，而不是 /api/ps。

**为什么不用 /api/ps**：2026-09-27 实测发现它会**报旧数** ——
显卡实际占用已经掉到 1196 MiB（模型被卸载了）时，`/api/ps` 依旧返回
`size_vram: 9691065219`。拿它当判据会得出完全相反的结论。

**权威字段**（`/api/chat` 的返回体里）：
  · `load_duration`        这一轮花在"加载模型"上的时间。**已加载就是 0（或极小）**
  · `prompt_eval_duration` 预填充耗时。**前缀缓存命中时会显著变小**
  · `eval_count`/`eval_duration`  生成速度
  · `total_duration`       这一轮总耗时

跑法：
    python scripts/probe_ollama_reload.py                     # 默认 qwen3-vl-think:30b
    python scripts/probe_ollama_reload.py --model qwen3-coder:30b
    python scripts/probe_ollama_reload.py --idle 60           # 中间空闲 60 秒再问一轮
"""
import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request

# ⚠️ 加 line_buffering：不加的话重定向到文件时会**整块缓存**，跑十分钟看不到一行，
#    很容易被误判成"卡住了"（2026-09-27 实测踩到）。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
OLLAMA = "http://127.0.0.1:11434"


def ps_names():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as r:
            return [str(m.get("name")) for m in (json.load(r).get("models") or [])]
    except Exception:
        return []


def ask(model, messages, keep_alive="4h", num_predict=24):
    """返回 (首个字节耗时, 指标 dict)。非流式拿不到"首字节"，所以用流式但只读第一段。"""
    body = {"model": model, "messages": messages, "stream": True,
            "keep_alive": keep_alive, "options": {"num_predict": num_predict}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    first_t = None
    metrics = {}
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("error"):
                    return None, {"error": str(obj["error"])[:160]}
                if first_t is None and ((obj.get("message") or {}).get("content")
                                        or (obj.get("message") or {}).get("thinking")):
                    first_t = time.time() - t0
                if obj.get("done"):
                    metrics = {k: obj.get(k) for k in
                               ("total_duration", "load_duration", "prompt_eval_count",
                                "prompt_eval_duration", "eval_count", "eval_duration")}
                    break
    except urllib.error.HTTPError as e:
        return None, {"error": "HTTP %s: %s" % (e.code, (e.read() or b"")[:160])}
    except Exception as e:
        return None, {"error": "%s: %s" % (type(e).__name__, e)}
    return first_t, metrics


def ms(v):
    return "%.0f ms" % (v / 1e6) if isinstance(v, int) else "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3-vl-think:30b")
    ap.add_argument("--idle", type=int, default=45, help="第 3 轮前故意空闲的秒数")
    ap.add_argument("--keep-alive", default="4h")
    args = ap.parse_args()

    print("模型：%s   保活：%s" % (args.model, args.keep_alive))
    print("（load_duration = 这一轮花在「加载模型」上的时间；已加载就是 0）")
    print("-" * 78)
    print("%-6s %-10s %-12s %-12s %-10s %-12s %s"
          % ("轮次", "首段", "load", "prompt_eval", "tokens", "生成", "备注"))
    print("-" * 78)

    h = [{"role": "user", "content": "1+1 等于几？只回一个数字。"}]
    rows = []
    for i in (1, 2):
        ft, m = ask(args.model, h, args.keep_alive)
        if m.get("error"):
            print("第 %d 轮失败：%s" % (i, m["error"]))
            return 1
        rows.append((i, ft, m))
        print("%-6s %-10s %-12s %-12s %-10s %-12s %s"
              % ("第%d轮" % i,
                 "%.2fs" % ft if ft else "-",
                 ms(m.get("load_duration")),
                 ms(m.get("prompt_eval_duration")),
                 m.get("prompt_eval_count") or "-",
                 ms(m.get("eval_duration")),
                 "ps=%s" % (ps_names() or "无")))
        h += [{"role": "assistant", "content": "2"},
              {"role": "user", "content": "那 %d 呢？" % (i * 0 + 2)}]

    print("-" * 78)
    print("空闲 %d 秒（模拟用户看着屏幕想事情 / 去干别的）…" % args.idle)
    time.sleep(args.idle)
    h = h[:-1] + [{"role": "user", "content": "3+3 呢？"}]
    ft, m = ask(args.model, h, args.keep_alive)
    if m.get("error"):
        print("第 3 轮失败：%s" % m["error"])
        return 1
    rows.append((3, ft, m))
    print("%-6s %-10s %-12s %-12s %-10s %-12s %s"
          % ("第3轮", "%.2fs" % ft if ft else "-", ms(m.get("load_duration")),
             ms(m.get("prompt_eval_duration")), m.get("prompt_eval_count") or "-",
             ms(m.get("eval_duration")), "ps=%s" % (ps_names() or "无")))
    print("-" * 78)

    ok = True
    for i, ft, m in rows[1:]:
        ld = m.get("load_duration") or 0
        if ld > 1_000_000_000:      # > 1 秒 = 真的重载了
            print("[!!] 第 %d 轮发生了**模型重载**：load_duration=%s" % (i, ms(ld)))
            ok = False
        else:
            print("[OK] 第 %d 轮没有重载（load_duration=%s）" % (i, ms(ld)))
    print()
    print("结论：%s" % ("多轮之间模型一直驻留，未发生重载 ✅" if ok
                        else "存在重载 ❌ —— 需要排查是谁把模型踢出去了"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
