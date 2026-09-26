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
kept, dropped, _cl0 = M._trim_history_to_budget(fake_history(30), sysp, sch, CFG)
check("30 条历史至少保留 10 条（改前只有 2 条）", len(kept) >= 10,
      "保留 %d 条 / 丢掉 %d 条" % (len(kept), len(dropped)))
check("常量 MIN_HISTORY_TOKENS 存在且 > 1500",
      getattr(M, "MIN_HISTORY_TOKENS", 0) > 1500, getattr(M, "MIN_HISTORY_TOKENS", None))
check("摘要预留也算进预算（DIGEST_RESERVE）", getattr(M, "DIGEST_RESERVE", 0) > 0)

# 极端配置（把输出预留调到最大）也不许把历史挤没
_c = dict(CFG)
_c["max_tokens"] = 16384
kept2, _d2, _cl2 = M._trim_history_to_budget(fake_history(30), sysp, sch, _c)
check("把 max_tokens 拉到 16384 仍保留 ≥5 条（宁可少留输出、也不能没记忆）",
      len(kept2) >= 5, "保留 %d 条" % len(kept2))

print()
print("=" * 68)
print("② 图片要算 token（这是 400 报错的根因）")
t512 = M._image_tokens([png_b64(512, 512)])
t1024 = M._image_tokens([png_b64(1024, 1024)])
t2048 = M._image_tokens([png_b64(2048, 2048)])
check("512 图有非零估算", t512 > 0, t512)
# ⚠️⚠️ 2026-09-26 按**实测**改正这条断言。
#   原来写的是"越大的图 token 越多（512 < 1024 < 2048）"——那是照公式推的，
#   而 Ollama 实报的是：512×512 与 1024×1024 **都是 1028 token**
#   （它会先把图规整到自己的目标分辨率，所以**这一档之内大小不影响**）。
#   所以正确的判据是"1024 以内按张计费、超过才放大"。
check("1024 以内按张计费（512 与 1024 同价，实测如此）",
      t512 == t1024, "%d / %d" % (t512, t1024))
check("超过 1024 才按面积放大（2048 > 1024）", t2048 > t1024,
      "%d / %d / %d" % (t512, t1024, t2048))
check("单张 1024 图估算 ≈1030（Ollama 实报 1028）", 1000 <= t1024 <= 1060, t1024)
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
kept3, _d3, _cl3 = M._trim_history_to_budget(fake_history(30), sysp, sch, CFG,
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
print("⑦ ★ 用户**自己粘贴的长文**也要装得下（改前会直接把窗口顶爆）")
# ⚠️ 实测的缺口（2026-09-26）：裁剪原来只丢**历史**，而"用户这一条"永远保留 ——
#    粘贴一篇 4 万字的文档时，提示词 = 3.1 万(输入) + 1.7 万(系统提示+工具)
#    = 4.8 万 > num_ctx 24576，Ollama 直接回 400，用户只拿到一句报错。
#    修法：单条消息也截断（保头保尾），并把截断事实**告诉用户**。
_HUGE = "这是一段很长的资料，用来测试超长输入。" * 2000        # 约 4 万字
_keptH, _dropH, _clH = M._trim_history_to_budget(
    [{"role": "user", "content": _HUGE}], sysp, sch, CFG)
_overhead = (M._est_tokens(sysp)
             + M._est_tokens(json.dumps(sch, ensure_ascii=False)))
_total = _overhead + sum(M._est_tokens(m.get("content") or "") for m in _keptH)
_ctx = int(CFG.get("num_ctx") or 8192)
check("超长输入被截断（不再原样顶爆窗口）",
      len(_keptH[0]["content"]) < len(_HUGE),
      "%d 字 → %d 字" % (len(_HUGE), len(_keptH[0]["content"])))
check("截断后提示词**不再超窗**", _total <= _ctx,
      "约 %d token / num_ctx=%d" % (_total, _ctx))
check("返回了截断信息（调用方据此告知用户）", bool(_clH) and _clH.get("cut", 0) > 0,
      str(_clH))
check("保头保尾（开头和结尾都在）",
      _HUGE[:12] in _keptH[0]["content"] and _HUGE[-12:] in _keptH[0]["content"])
check("截断处有明确标记（写明省略了多少字）", "省略" in _keptH[0]["content"])
_keptS, _dS, _clS = M._trim_history_to_budget(
    [{"role": "user", "content": "帮我做份 PPT"}], sysp, sch, CFG)
check("⚠️ 正常长度的消息**一个字都不动**",
      _keptS[0]["content"] == "帮我做份 PPT" and _clS is None, str(_clS))
check("图片超过 6 张也要全算进预算（改前只算前 6 张）",
      M._image_tokens(["x"] * 12) > M._image_tokens(["x"] * 6) > 0,
      "%d → %d" % (M._image_tokens(["x"] * 6), M._image_tokens(["x"] * 12)))
check("源码里有「告知用户输入被截断」的分支",
      "_clamped[" in SRC_MAIN or "_clamped" in SRC_MAIN)

print()
print("=" * 68)
print("⑧ 属性：微改出来的图要标成「AI 微改」（原来显示成「图片」）")
check("edit_image 的 ui 事件带 origin=edit", '"origin": "edit"' in SRC_TOOLS)
check("前端认这个 origin", 'ui.origin === "edit"' in JS and "AI 微改" in JS)

print()
print("=" * 68)
print("⑨ 附件去重：拖一张别贴成两张")
check("同一批按 名字+大小+修改时间 去重",
      "f.name || \"\"" in JS and "f.size || 0" in JS and "seen.has(key)" in JS)
check("跨批次按内容（dataURL）去重", "images.indexOf(src) >= 0" in JS)
check("重复时给用户一句提示（别静默吞掉）",
      "这张图已经在附件里了" in JS)

print()
print("=" * 68)
print("⑩ 工具别抢活：搜图不许顶替生图/微改")
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
print("⑪ ★ 工具轮之间要再核一次预算（越跑越长会顶爆窗口）")
# ⚠️ 实测的缺口（2026-09-26）：只在整轮开始前裁过一次历史和输入，但**工具轮越跑越长** ——
#    每轮都把工具结果追加进 working（web_read 一篇网页 9000 字 ≈ 6700 token），
#    多跑两轮必然顶爆窗口；顶爆后的兜底是"砍到只剩最后两条"，等于把刚查到的材料全丢了，
#    用户体感就是"查了却没回答"。
_wsys = {"role": "system", "content": "x" * 9000}
_working = [_wsys,
            {"role": "user", "content": "帮我查一下"},
            {"role": "assistant", "content": "我查一下"},
            {"role": "tool", "content": "网页正文。" * 3000, "tool_name": "web_read"},
            {"role": "assistant", "content": "再查一篇"},
            {"role": "tool", "content": "网页正文。" * 3000, "tool_name": "web_read"}]
_ctx = int(CFG.get("num_ctx") or 8192)
_over = (M._est_tokens(json.dumps(sch, ensure_ascii=False))
         + sum(M._est_tokens(m.get("content") or "") for m in _working))
check("造出来的场景**确实超窗**（否则这条测试没意义）", _over > _ctx,
      "约 %d / %d" % (_over, _ctx))
_after = M._shrink_round_prompt(_working, CFG, sch)
_after_all = _after + M._est_tokens(json.dumps(sch, ensure_ascii=False))
check("压缩后不再超窗", _after_all <= _ctx, "约 %d / %d" % (_after_all, _ctx))
check("system 消息**永不丢**（它是唯一的规则来源）",
      any(m.get("role") == "system" for m in _working))
check("长工具结果被截断（而不是整条丢掉）",
      any(m.get("role") == "tool" for m in _working)
      and max(len(m.get("content") or "") for m in _working) < 17999,
      "最长 %d 字" % max(len(m.get("content") or "") for m in _working))
check("至少留下 3 条（别把本轮也丢光）", len(_working) >= 3, "%d 条" % len(_working))
check("常量 ROUND_MSG_MAX_TOKENS 存在且合理",
      1000 <= getattr(M, "ROUND_MSG_MAX_TOKENS", 0) <= 8000,
      getattr(M, "ROUND_MSG_MAX_TOKENS", None))
check("循环里每轮真的调了它（不是写了没用）",
      "_shrink_round_prompt(working" in SRC_MAIN)

print()
print("=" * 68)
print("⑫ ★ 带图提问曾经必崩（工具 schema 把窗口吃光了）—— 三条修复都要在")
# ⚠️⚠️ 实测复盘（2026-09-26）：用户在对话里**拖 3 张图**提问 → Ollama 直接 400
#   `request (25383 tokens) exceeds the available context size (24576)`。
#   用 Ollama 实报的 token 数一算就清楚了：
#      39 个工具的 schema = 15595 token，系统提示 ≈6600，3 张图 = 3090(=3×1030)
#      ⇒ 光这些就 25000+，留给"思考+回答"的**是负数**。
#   三条修法缺一不可，这里逐条钉住：
#     ① 图片 token 按实测校准（原来公式算 342/张，**实际 1028** —— 低估 3 倍）；
#     ② 带图的轮次自动精简工具集（砍地图/工作区/天气/上传代码托管 ≈省 3900）；
#     ③ 上下文窗口 24576 → **28672**（实测仍 100% 驻显存、速度不变，白送 4096）。
_img_b64 = [png_b64(1024, 1024)]
_per = M._image_tokens(_img_b64)
check("★ 图片 token 按实测估（1028/张，不再低估 3 倍）",
      1000 <= _per <= 1060, "估成 %d token/张" % _per)
check("多张图按张数线性累加", M._image_tokens(_img_b64 * 3) == _per * 3,
      "3 张 → %d" % M._image_tokens(_img_b64 * 3))
check("ctx 默认 ≥ 28672（24576 时带 3 张图必超）",
      int(CFG.get("num_ctx") or 0) >= 28672, "num_ctx=%s" % CFG.get("num_ctx"))

_full = T.make_schemas(True, True, True, ask_mode="deep")
_lean = T.make_schemas(True, True, True, ask_mode="deep", lean=True)
_t_full = M._est_tokens(json.dumps(_full, ensure_ascii=False))
_t_lean = M._est_tokens(json.dumps(_lean, ensure_ascii=False))
check("带图精简集比完整集小（省得出来）", _t_lean < _t_full - 3000,
      "%d → %d（省 %d）" % (_t_full, _t_lean, _t_full - _t_lean))
_ln = [x["function"]["name"] for x in _lean]
check("精简集**保住**图片与文档生成工具",
      all(x in _ln for x in ("generate_image", "edit_image", "compose_images",
                             "web_image_search", "make_pptx", "make_docx",
                             "make_xlsx", "edit_office")))
check("精简集砍掉本轮用不到的地图/工作区/天气/上传",
      not any(x in _ln for x in ("map_plan", "nearby_places", "connect_amap",
                                 "get_weather", "github_push"))
      and not any(x.startswith("workspace_") for x in _ln))
# 带 3 张图的实际占用要能装进窗口、并给输出留出空间
_sys_real = 6600                         # 系统提示的实测占用（Ollama 实报 ≈6600）
_tot3 = _t_lean + _sys_real + M._image_tokens(_img_b64 * 3)
check("★ 3 张图 + 精简集 + 系统提示 装得进 num_ctx，且留有输出余量",
      _tot3 < int(CFG.get("num_ctx") or 0) - 2000,
      "约 %d / %d（余 %d）" % (_tot3, CFG.get("num_ctx"),
                              int(CFG.get("num_ctx")) - _tot3))

# 消息里明说要地图时不能裁（否则用户发现"工具不见了"）
_src_main2 = SRC_MAIN
check("带图轮的精简带了关键词豁免（用户说要地图/工作区就不裁）",
      "_keep_rich" in _src_main2 and "地图" in _src_main2 and "workspace" in _src_main2)
check("重试时也沿用精简集（别一刀切回全集）",
      "lean=_lean" in _src_main2, "出现 %d 次" % _src_main2.count("lean=_lean"))

print()
print("=" * 68)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
