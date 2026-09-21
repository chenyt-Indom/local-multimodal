# -*- coding: utf-8 -*-
"""守住"等的时候要告诉用户在等什么"这条体验。

背景（用户 2026-09-22 拿着截图问："prompt 发出去好久才响应，思考才憋出两个字，
然后又卡着不动了，正常吗？"）：
  · 截图那次实测是 02:36:40 发出、02:36:55 完成（15.2 秒）——**不是卡死**，
    只是老的"每轮重算 2 万 token"（已修）；
  · 但顺着查出一个**真实故障**：Ollama 一次只服务一个生成，
    两条请求并发时，先发的那条会在吐 1 个思考字之后**静默 26 秒**（实测复现）。
    之前这段时间界面只有一个不动的「思考中…」，用户根本分不清"在算"还是"死了"。

跑法：python test_slow_feedback.py   （必须用 Python 3.14）
"""
import asyncio
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_slowfb_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

import json  # noqa: E402

from backend import main as M  # noqa: E402

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


async def _drain(agen):
    return [c async for c in agen]


async def _fake(payload):
    yield payload


JS = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


print("=" * 62)
print("① 后端：只在「确实在等」的时候才说话（不打扰）")
M._chat_busy = 0
M._extracting = False
out = run(_drain(M._track_chat(_fake('{"text": "hi"}\n'))))
check("无人占用 → 不插任何提示", out == ['{"text": "hi"}\n'], str(out))
check("跑完之后计数归零（不会越积越多）", M._chat_busy == 0, M._chat_busy)

print()
print("=" * 62)
print("② 后端：别的聊天正在跑 → 提前说明「要等」")
M._chat_busy = 1                      # 假装已经有一条在生成
out = run(_drain(M._track_chat(_fake('{"text": "hi"}\n'))))
notes = [json.loads(c)["note"] for c in out if c.strip().startswith('{"note"')]
check("插了一条「还在生成、要等它让出来」的提示", bool(notes), str(out)[:120])
check("提示里说清了原因（模型一次只跑一个）",
      notes and "一次只跑一个" in notes[0], str(notes))
check("正文照旧透传（提示不吞内容）", any('"text"' in c for c in out))
M._chat_busy = 0

print()
print("=" * 62)
print("③ 后端：后台在整理记忆 → 用另一句（两条提示别打架）")
M._chat_busy = 0
M._extracting = True
try:
    out = run(_drain(M._track_chat(_fake('{"text": "hi"}\n'))))
finally:
    M._extracting = False
notes = [json.loads(c)["note"] for c in out if c.strip().startswith('{"note"')]
check("提示说的是「整理记忆」", notes and "记忆" in notes[0], str(notes))
check("两种提示互斥（不同时冒两条）", len(notes) == 1, str(notes))

print()
print("=" * 62)
print("④ 后端：登记/让路逻辑没被改坏")
check("_chat_started 返回「是否已有别的聊天在跑」",
      M._chat_started() is False and M._chat_started() is True)
check("让路信号会被置起（提炼要主动断开）", M._extract_abort is True)
M._chat_finished()
M._chat_finished()
check("计数回到 0", M._chat_busy == 0, M._chat_busy)

print()
print("=" * 62)
print("⑤ 前端：没有数据进来时要报出「已等待多久」")
check("有卡顿看门狗（_armStall / _clearStall）",
      "_armStall" in JS and "_clearStall" in JS)
check("提示里带已等待秒数", "已等待 " in JS and "模型正在准备" in JS)
check("提示解释了可能的原因（要预填充 / 前面还有任务）",
      "大提示词要先读一遍" in JS and "还有任务在跑" in JS)
check("⚠️ 每收到一块数据就重新计时（不会误报）",
      "_armStall();                 // 收到数据" in JS)
check("⚠️ 收尾时一定要停掉计时器（否则会一直刷）",
      "_clearStall();                 // 别忘了停掉卡顿看门狗" in JS)
check("提示是写在思考状态那一行（用户正盯着那里）", "thinkStatus.textContent" in JS)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
