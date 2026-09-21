# -*- coding: utf-8 -*-
"""验证「跨轮串扰」：上一轮的东西不能被悄悄接到新话题上。

用户报的现象（2026-09-22）：
    「我上一轮让它给我生成一幅水母的图片，但是我下一轮让它给我生成一段旅行计划，
      结果模型给了一张水母的图片」——上下轮毫无关联，却沿用了上轮的东西。

排查到的**结构性根因**（代码侧，能确证的那部分）：
    `_LAST_IMAGE` 的有效期是 **1 小时**，而 `ctx["images"]` 写的是
    `images or _recent_image()` —— 等于**用户拖过一次图之后的整整一小时里，
    每一轮都会被系统当成"这一轮的图"**。三个后果：
      ① 全新话题也会拿那张旧图当底图（微改/参考图）；
      ② `writing_mode` 被旧图挡掉（`and not _recent_image()`）→ 写长文退化成普通问答；
      ③ `simple_q` 不收紧 → 白等更久。

现在的规矩：**只有用户明确指向上一张图时才复用**（`_prev_image_for`）。
另外补了一条提示词规则，让模型自己判"这句是接着说上文，还是新话题"。

⚠️ 判据必须**双条件**：指代词/改图动作单独出现都不算 ——
   「帮我写份文档，**换成** Word」里的"换成"跟图片毫无关系，
   只看动词会把旧图接到文档任务上（那是另一种串扰）。

跑法（不占端口、不联网）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_cross_turn.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["MM_DATA_DIR"] = tempfile.mkdtemp(prefix="mm_crossturn_")
sys.path.insert(0, ROOT)

import backend.main as M            # noqa: E402
from backend import tools as T      # noqa: E402

PASS, FAIL = 0, []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {extra}")


print("\n=== 1. 用户报的那句：全新话题绝不能复用上一张图 ===")
MUST_NOT = [
    "帮我生成一段旅行计划",              # ← 用户原话
    "帮我生成一份旅行计划",
    "给我一份旅行计划",
    "帮我画一只水母",                    # 新的一张图，不是改上一张
    "用 Python 写一个去重函数",
    "帮我写份文档，导出成 Word",          # 含"导出/换成"类动作，但与图片无关
    "帮我把这份表格换成 Excel 格式",
    "帮我调一下这段代码的性能",
    "今天天气怎么样",
    "",
]
for t in MUST_NOT:
    check(f"{t[:22]!r} → 不复用旧图", not M._refers_to_prev_image(t))

print("\n=== 2. 真的在说「那张图」时必须认得出来（否则微改就废了）===")
MUST = [
    "把这张图改成红色",
    "刚才那张图再亮一点",
    "这张图片放大一点",
    "把背景换成蓝色",
    "把 logo 去掉",
    "改成红色",
    "帮我调一下这张图的尺寸",
    "参考图用刚才那张",
]
for t in MUST:
    check(f"{t[:22]!r} → 复用旧图", M._refers_to_prev_image(t))

print("\n=== 3. 行为：_prev_image_for 只看这一轮的话 ===")
# 手工塞一张"上一轮的图"（模拟用户一小时前拖进来的）
M._LAST_IMAGE["b64"] = "data:image/png;base64,AAAA"
M._LAST_IMAGE["ts"] = __import__("time").time()
check("（前置）旧图确实还在有效期内", len(M._recent_image()) == 1)
check("★ 新话题 → 拿不到旧图", M._prev_image_for("帮我生成一段旅行计划") == [])
check("★ 指上图 → 拿得到", len(M._prev_image_for("把这张图改成红色")) == 1)
# 过期之后一律拿不到
M._LAST_IMAGE["ts"] = 0
check("过期之后谁都拿不到", M._prev_image_for("把这张图改成红色") == [])
M._LAST_IMAGE["b64"] = None

print("\n=== 4. 没有图时微改工具必须「明确报错」，而不是悄悄用错图 ===")
r = T.dispatch("edit_image", {"prompt": "make it red"}, [], {"images": []})
check("给出可读的报错（让用户重拖/给路径）", "无法确定" in r or "拖入" in r, r[:60])

print("\n=== 5. 提示词：让模型自己判「接着说 vs 新话题」 ===")
src = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
check("有「接着说 / 新话题」的判断规则", "这是在接着说上面那件事" in code)
check("⚠️ 明确写了「上一轮生成过图片 ≠ 这一轮还要图片」",
      "上一轮生成/搜过图片 ≠ 这一轮还要图片" in code)
check("新话题时不许端出旧产物（文件/表格同理）",
      "这一轮还要写文件" in code)
check("拿不准就按新话题做，或问一句", "宁可" in code or "ask_user 问一句" in code)
check("记忆/知识库也只在相关时用", "确实相关时才用" in code)

print("\n=== 6. 代码侧：不该再有「无条件复用旧图」的地方 ===")
# 三处原来都用了 no-recent_image
check("ctx 用的是 gated 的 prev_img",
      '"images": images or prev_img' in code)
check("writing_mode 用的是 gated 的 prev_img",
      "and not images and not prev_img)" in code)
check("simple_q 用的是 gated 的 prev_img",
      "and not images and not prev_img and not web_on" in code)
# _recent_image() 只应出现在 _prev_image_for 里（唯一入口）；定义行不算
uses = [l.strip() for l in code.splitlines()
        if "_recent_image()" in l and not l.strip().startswith("def ")]
check("_recent_image() 只在 _prev_image_for 内被调用",
      all("return _recent_image()" in l for l in uses),
      f"实得 {uses}")

print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  ! " + f)
sys.exit(1 if FAIL else 0)
