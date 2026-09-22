# -*- coding: utf-8 -*-
"""守住用户 2026-09-22 12:49 报的一串问题（文生图 / 图片链路 / 失忆）。

用户原话与实测根因：

① **失忆**：`max_tokens=8192` + 系统提示与工具 16025 token + 联网预留 3800 之后，
   `24576 − 8192 − 16025 − 3800 − 512 = −3953`（负数）→ 走兜底分支，
   **历史每轮只剩最后 2 条**，其余压成 743 字摘要。
② **Ollama 400**：`request (26352 tokens) exceeds the available context size (24576)`
   —— 附件图片**完全没算 token**（图片挂在最后一条 user 消息上，2048×2048 一张就 1300+）。
③ **"模型找不到刚才生成的图"**：只有"用户自己拖进来的图"才会被 `_remember_image` 记住，
   生成/微改出来的图**没登记**，所以下一轮说「微改刚才那张」取不到底图。
④ **拖一张图贴成两张**：拖拽/剪贴板源有时给同一文件多个表示，前端没有去重。
⑤ **微改/生图 被做成搜图**：工具描述没有"别抢活"的硬约束。

跑法：python test_context_and_image_flow.py   （必须用 Python 3.14）
"""
import ast
import base64
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_ctx_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from PIL import Image                       # noqa: E402

from backend import config as C             # noqa: E402
from backend import main as M               # noqa: E402
from backend import tools as T              # noqa: E402

CFG = C.load_config()
SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
SRC_TOOLS = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


def png_b64(w, h, color="red", fmt="PNG"):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format=fmt)
    return base64.b64encode(b.getvalue()).decode()


def fake_history(n):
    out = []
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": "第%d轮：这是一句普通的中文问题，用来占位。" % i})
    return out


sysp = M._SystemPrompt.build("你好", "", None)
sch = T.make_schemas(True, True, True, writing=False)

print("=" * 68)
print("① 失忆：历史不能再被「输出预留」挤成 2 条")
kept, dropped = M._trim_history_to_budget(fake_history(30), sysp, sch, CFG)
check("30 条历史至少保留 10 条（改前只有 2 条）", len(kept) >= 10,
      "保留 %d 条 / 丢掉 %d 条" % (len(kept), len(dropped)))
check("常量 MIN_HISTORY_TOKENS 存在且 > 1500",
      getattr(M, "MIN_HISTORY_TOKENS", 0) > 1500, getattr(M, "MIN_HISTORY_TOKENS", None))
check("摘要预留也算进预算（DIGEST_RESERVE）", getattr(M, "DIGEST_RESERVE", 0) > 0)

# 极端配置（把输出预留调到最大）也不许把历史挤没
_c = dict(CFG)
_c["max_tokens"] = 16384
kept2, _ = M._trim_history_to_budget(fake_history(30), sysp, sch, _c)
check("把 max_tokens 拉到 16384 仍保留 ≥5 条（宁可少留输出、也不能没记忆）",
      len(kept2) >= 5, "保留 %d 条" % len(kept2))

print()
print("=" * 68)
print("② 图片要算 token（这是 400 报错的根因）")
t512 = M._image_tokens([png_b64(512, 512)])
t1024 = M._image_tokens([png_b64(1024, 1024)])
t2048 = M._image_tokens([png_b64(2048, 2048)])
check("512 图有非零估算", t512 > 0, t512)
check("越大的图 token 越多（512 < 1024 < 2048）", t512 < t1024 < t2048,
      "%d / %d / %d" % (t512, t1024, t2048))
check("2048 图估算 ≥1000（按 28×28 patch 折算）", t2048 >= 1000, t2048)
check("空图片列表 = 0", M._image_tokens([]) == 0 and M._image_tokens(None) == 0)
check("取不到尺寸时也有兜底估算（不乱码就返回正数）",
      M._image_tokens(["not-a-real-image"]) > 0)

print()
print("=" * 68)
print("③ 超大图发给模型前要压到 1024（省 3/4 token，原件不动）")
big = png_b64(2048, 2048, "blue")
small = M._shrink_for_model([big])[0]
with Image.open(io.BytesIO(base64.b64decode(small))) as im:
    ms = max(im.size)
check("2048 → 长边 1024", ms == 1024, ms)
check("压缩后 token 明显下降（≤ 原来一半）",
      M._image_tokens([small]) <= M._image_tokens([big]) // 2,
      "%d → %d" % (M._image_tokens([big]), M._image_tokens([small])))
tiny = png_b64(256, 256)
check("小图原样返回（不做无谓重编码）", M._shrink_for_model([tiny])[0] == tiny)
check("不是图片的项原样保留（宁可多占也别丢）",
      M._shrink_for_model(["garbage"])[0] == "garbage")

print()
print("=" * 68)
print("④ 预算里要真的把图片算进去")
kept3, _ = M._trim_history_to_budget(fake_history(30), sysp, sch, CFG,
                                     images_b64=[png_b64(2048, 2048)])
check("同一段历史 + 一张大图 → 保留条数变少（说明图占了额度）",
      len(kept3) <= len(kept), "%d → %d" % (len(kept), len(kept3)))

print()
print("=" * 68)
print("⑤ 真撞上「超出窗口」要自动精简重试，而不是把英文报错甩给用户")
_real = ('{"error":{"code":400,"message":"request (26352 tokens) exceeds the available '
         'context size (24576 tokens), try increasing it",'
         '"type":"exceeded_context_size_error","param":26352,"n_ctx":24576}}')
check("认得 Ollama 的 exceeded_context_size_error", M._is_context_err(_real) is True)
check("认得中文/其它写法里的 n_ctx 超限",
      M._is_context_err("prompt exceeds n_ctx") is True)
check("普通错误不会被误判（模型不存在）",
      M._is_context_err("model 'x' not found") is False)
check("源码里有「自动精简后重试」的分支",
      "_ctx_trimmed" in SRC_MAIN and "已自动精简后重试" in SRC_MAIN)

print()
print("=" * 68)
print("⑥ 「微改刚才生成的图」要能取到底图")
check("生成/微改的图会被登记成「最近一张图」",
      'e.get("origin") in ("gen", "edit")' in SRC_MAIN and "_remember_image([e[\"b64\"]])" in SRC_MAIN)
check("「帮我微改一下」这种不带「图」字的短句也认",
      M._refers_to_prev_image("帮我微改一下") is True)
check("「微改这张图片」认", M._refers_to_prev_image("微改这张图片") is True)
check("⚠️「帮我写份文档，换成 Word」**不许**认成图片任务",
      M._refers_to_prev_image("帮我写份文档，换成 Word") is False)
check("⚠️「生成一份旅行计划」不许认成图片任务",
      M._refers_to_prev_image("生成一份旅行计划，5 天") is False)
M._LAST_IMAGE["b64"] = None
M._remember_image(["FAKEB64"])
check("_recent_image() 能取回刚登记的那张", M._recent_image() == ["FAKEB64"])
M._LAST_IMAGE["b64"] = None

print()
print("=" * 68)
print("⑦ 属性：微改出来的图要标成「AI 微改」（原来显示成「图片」）")
check("edit_image 的 ui 事件带 origin=edit", '"origin": "edit"' in SRC_TOOLS)
check("前端认这个 origin", 'ui.origin === "edit"' in JS and "AI 微改" in JS)

print()
print("=" * 68)
print("⑧ 附件去重：拖一张别贴成两张")
check("同一批按 名字+大小+修改时间 去重",
      "f.name || \"\"" in JS and "f.size || 0" in JS and "seen.has(key)" in JS)
check("跨批次按内容（dataURL）去重", "images.indexOf(src) >= 0" in JS)
check("重复时给用户一句提示（别静默吞掉）",
      "这张图已经在附件里了" in JS)

print()
print("=" * 68)
print("⑨ 工具别抢活：搜图不许顶替生图/微改")
wsch = [s for s in sch if s["function"]["name"] == "web_image_search"][0]["function"]["description"]
esch = [s for s in sch if s["function"]["name"] == "edit_image"][0]["function"]["description"]
check("搜图描述里有「不许抢活」的硬约束", "不许抢 edit_image" in wsch)
check("搜图描述点名：说「微改/改成」且有图 → 用 edit_image",
      "edit_image" in wsch and "微改" in wsch)
check("搜图描述点名：说「画/生成」→ 用 generate_image",
      "generate_image" in wsch)
check("微改描述里说清「不用重拖、也不用填 source」",
      "不用填 source" in esch and "不要让他重新拖一次" in esch)
check("微改描述里明确「不要改用搜图」", "不要改用搜图" in esch)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 68)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
