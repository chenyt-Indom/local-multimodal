# -*- coding: utf-8 -*-
"""只验一件事：**画完图之后，紧接着聊天会不会卡**。

为什么要单独一个脚本：`verify_live.py` 会把整轮跑完，而"工具返回后模型还要写一段
图中总结"那段时间可能非常长（实测某一轮 997.6 秒），把真正要看的数字淹掉。
这里模拟用户真实行为 —— **看到图片就停下、去看图、然后接着打字**。

跑法（必须 Python 3.14）：
    python scripts/verify_draw_then_chat.py
    python scripts/verify_draw_then_chat.py --look 30    # "看图"的秒数
"""
import argparse
import io
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ⚠️ 加 line_buffering：不加的话重定向到文件时会**整块缓存**，跑十分钟看不到一行，
#    很容易被误判成"卡住了"（2026-09-27 实测踩到）。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

BASE = "http://127.0.0.1:8000"
OLLAMA = "http://127.0.0.1:11434"
SESSION = "verify-draw-%d" % int(time.time())


def gpu_mib():
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=8).stdout.split()
        return int(o[0]) if o else -1
    except Exception:
        return -1


def ps():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as r:
            return {str(m.get("name")): int(m.get("size_vram") or 0)
                    for m in (json.load(r).get("models") or [])}
    except Exception:
        return {}


def chat(messages, on_event=None, stop=None, max_wait=900):
    """读 NDJSON 流。stop(event_dict) 返回 True 时**主动断开**（模拟用户不等了）。"""
    body = {"messages": messages, "stream": True, "session_id": SESSION}
    req = urllib.request.Request(BASE + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    first_t, image, status_msg, tools = None, None, None, []
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
                if obj.get("status") and not status_msg:
                    status_msg = obj["status"]
                if obj.get("tool_start"):
                    tools.append(str((obj["tool_start"] or {}).get("name")))
                ui = obj.get("ui") or {}
                if ui.get("type") == "image" and ui.get("b64"):
                    import base64
                    image = base64.b64decode(ui["b64"])
                if obj.get("error"):
                    return {"err": str(obj["error"])[:200], "first_t": first_t,
                            "image": image, "status_msg": status_msg, "tools": tools,
                            "elapsed": time.time() - t0}
                msg = obj.get("message") or {}
                if first_t is None and (msg.get("thinking") or msg.get("content")):
                    first_t = time.time() - t0
                if on_event:
                    on_event(obj, time.time() - t0)
                if stop and stop(obj):
                    break
    except Exception as e:
        return {"err": "%s: %s" % (type(e).__name__, e), "first_t": first_t,
                "image": image, "status_msg": status_msg, "tools": tools,
                "elapsed": time.time() - t0}
    return {"err": None, "first_t": first_t, "image": image,
            "status_msg": status_msg, "tools": tools, "elapsed": time.time() - t0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--look", type=int, default=30, help="模拟用户看图/读结果的秒数")
    args = ap.parse_args()

    print("=" * 70)
    print("0. 初始状态：GPU %d MiB   Ollama %s" % (gpu_mib(), ps() or "（空）"))
    print("=" * 70)

    # ── 1. 让它画一张，只看图片事件，收到就断开（模拟"看到图就去看了"）──────
    print("\n1. 发起画图，一收到图片就断开（不等它写图中的总结）")
    t_img = {}

    def on_ev(obj, t):
        ui = obj.get("ui") or {}
        if ui.get("type") == "image":
            t_img["t"] = t

    h = [{"role": "user",
          "content": "直接生成图片，不要提问、不要与我确认：一只戴帽子的柴犬，写实风格。"
                     "请立刻调用画图工具生成。"}]
    r = chat(h, on_event=on_ev, stop=lambda o: bool(o.get("ui", {}).get("type") == "image"))
    if r["image"]:
        print("   ✅ 拿到图片：%.2f MB   图片出现在 %.1fs" % (len(r["image"]) / 1048576.0,
                                                     t_img.get("t", -1)))
    else:
        print("   ❌ 没拿到图片：%s" % r["err"])
        return 1
    print("   工具调用：%s" % (r["tools"] or "（无）"))
    print("   断开时状态：GPU %d MiB   Ollama %s" % (gpu_mib(), ps() or "（空）"))

    # ── 2. 模拟用户看图 ────────────────────────────────────────────────
    print("\n2. 模拟用户看图（%d 秒）—— 后台应该在预热对话模型" % args.look)
    for i in range(args.look // 5):
        time.sleep(5)
        print("     +%2ds  GPU %5d MiB   Ollama %s"
              % ((i + 1) * 5, gpu_mib(),
                 ",".join("%s(%.1fGB)" % (k, v / 1073741824.0)
                          for k, v in ps().items()) or "（空）"))

    # ── 3. 接着聊 ─────────────────────────────────────────────────────
    print("\n3. 接着聊天（用户不重启任何东西，直接打字）")
    h2 = h + [{"role": "assistant", "content": "（已生成图片）"},
              {"role": "user", "content": "刚才那张图里是什么动物？一句话。"}]
    r2 = chat(h2, stop=lambda o: bool((o.get("message") or {}).get("content")))
    print("   第 1 条  首段 %.2fs   %s"
          % (r2["first_t"] or -1,
             ("后端提示：" + r2["status_msg"]) if r2["status_msg"] else "（无加载提示）"))
    if r2.get("err"):
        print("   ⚠️ 出错：%s" % r2["err"])
    h2 += [{"role": "assistant", "content": "柴犬"},
           {"role": "user", "content": "它戴的是什么？"}]
    r3 = chat(h2, stop=lambda o: bool((o.get("message") or {}).get("content")))
    print("   第 2 条  首段 %.2fs   %s"
          % (r3["first_t"] or -1, ("出错：" + r3["err"]) if r3.get("err") else ""))

    print()
    ok1 = (r2["first_t"] or 999) < 40           # 冷加载上限 ~25~30s
    ok2 = (r3["first_t"] or 999) < 5            # 热态应当秒回
    print("=" * 70)
    print("画完图后第 1 条：%.2fs  %s" % (r2["first_t"] or -1, "OK" if ok1 else "FAIL"))
    print("紧接着的第 2 条：%.2fs  %s" % (r3["first_t"] or -1, "OK" if ok2 else "FAIL"))
    print("（判据：第 1 条不该超过约 40 秒；第 2 条应当 <5 秒 = 没重载）")
    print("=" * 70)
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
