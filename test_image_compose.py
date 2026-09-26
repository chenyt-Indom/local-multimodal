# -*- coding: utf-8 -*-
"""守住「多图缝合」这个新能力（2026-09-26 用户提出）。

用户原话：「我给他几张图让它进行**缝合**并微改？」
此前**完全没有**这个能力（只有 edit_image 改单张），这里新增 compose_images。

★ 设计口径（断言就是照这个写的）：
  · 拼接走 PIL —— **像素级、不重绘**，所以接缝无缝、内容不走样、快（毫秒级）；
  · 「拼接后微改」是**可选的第二步**（img2img），幅度有**硬上限 0.45**
    （再大就不是"统一风格"而是"整张重画"，人物会变形）；
  · 「把 A 图里的人放进 B 图」这类**智能融合做不到** —— 提示里必须如实说明，
    不许模型假装做到（实测模型很爱编"已完成智能融合"）。
  · 出参要能下载（进生成文库）+ 能在「图片库」看到。

跑法：python test_image_compose.py   （纯 CPU，几秒）
"""
import glob
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_compose_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import t2i                       # noqa: E402
from backend import tools as T                # noqa: E402
from PIL import Image                         # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s  %s" % (name, detail))
    else:
        FAIL += 1
        print("  [!!] %s  %s" % (name, detail))


# ---- 造几张**尺寸/长宽比都不一样**的测试图（这样才测得出对齐与缩放） ----
IMGS = []
for i, (w, h, col) in enumerate(((640, 480, (200, 60, 60)), (480, 640, (60, 200, 60)),
                                 (800, 400, (60, 60, 200)), (500, 500, (220, 200, 60))), 1):
    p = os.path.join(TMP, "in%d.png" % i)
    Image.new("RGB", (w, h), col).save(p)
    IMGS.append(p)

print("=" * 66)
print("【1】三种排版的尺寸与格子（尺寸不齐也要拼得整齐）")
print("=" * 66)
r = t2i.compose(IMGS[:3], layout="horizontal", size=512)
check("横排：统一高度、宽度按比例", r.get("ok") and r["size"][1] == 512,
      "%s 格子%s" % (r.get("size"), r.get("cell")))
r2 = t2i.compose(IMGS[:3], layout="vertical", size=512)
check("竖排：统一宽度", r2.get("ok") and r2["size"][0] == 512, str(r2.get("size")))
r3 = t2i.compose(IMGS, layout="grid", size=512, cols=2)
check("网格 cols=2：2 行 2 列", r3.get("ok") and r3["size"][0] == r3["size"][1],
      str(r3.get("size")))
r4 = t2i.compose(IMGS, layout="grid", size=512)          # 4 张 → 自动 √4 = 2 列
check("网格自动列数（4 张 → 2×2）", r4.get("ok") and r4["size"] == r3["size"],
      str(r4.get("size")))
check("每格都按长边规整到 512（不超框）",
      all(max(w, h) <= 512 for (_, _, w, h) in r3["cells"]),
      str([(w, h) for (_, _, w, h) in r3["cells"]]))

print()
print("=" * 66)
print("【2】间距与底色（gap>0 要露出底色，不能糊成一片）")
print("=" * 66)
# ⚠️ 横排统一的是**高度**，每张宽度按各自比例缩放（我第一版断言按"宽度都一样"写，
#    算出来 702 vs 532 差一大截 —— 是断言错，不是代码错）。所以只比"有无间距"的差。
r5a = t2i.compose(IMGS[:2], layout="horizontal", size=256, gap=0, bg="FF0000")
r5b = t2i.compose(IMGS[:2], layout="horizontal", size=256, gap=20, bg="FF0000")
cw = r5b["cell"][0]
check("gap 反映到成品尺寸上（宽 = 无间距时 + 间距，高不变）",
      r5b["size"][0] == r5a["size"][0] + 20 and r5b["size"][1] == r5a["size"][1],
      "%s → %s（格宽 %d）" % (r5a["size"], r5b["size"], cw))
px = r5b["image"].load()
check("间距处确实是底色（红）", px[cw + 10, 10] == (255, 0, 0),
      str(px[cw + 10, 10]))
check("图片区域不是底色（说明图真的贴上去了）", px[10, 10] != (255, 0, 0),
      str(px[10, 10]))

print()
print("=" * 66)
print("【3】边界：不能崩，要给出人话")
print("=" * 66)
r6 = t2i.compose(IMGS[:1])
check("只有 1 张 → 拒绝并说明", not r6.get("ok") and "至少" in r6.get("error", ""),
      r6.get("error"))
r7 = t2i.compose([])
check("0 张 → 拒绝并说明", not r7.get("ok"), r7.get("error"))
r8 = t2i.compose(IMGS[:2], layout="乱写的排版名")
check("不认识的排版名 → 退回横排（不报错）", r8.get("ok") and r8["layout"] == "horizontal",
      r8.get("layout"))
r9 = t2i.compose(IMGS[:4], layout="grid", size=4096, cols=2)
check("成品过大 → 缩到长边 4096 以内", r9.get("ok") and max(r9["size"]) <= 4096,
      str(r9.get("size")))
_ev = []
_bad = T.dispatch("compose_images", {"sources": [IMGS[0], "C:/不存在/这张图.png"]}, _ev, {})
check("坏路径走工具入口 → 中文说明「找不到哪张」，不是英文异常",
      "找不到" in str(_bad) and "ascii" not in str(_bad).lower(), str(_bad)[:80])

print()
print("=" * 66)
print("【4】工具入口：落盘可下载 + 进图片库 + 回图片事件")
print("=" * 66)
_ev = []
_res = T.dispatch("compose_images", {"layout": "horizontal", "size": 384, "gap": 8,
                                     "sources": IMGS[:3], "filename": "缝合回归"},
                  _ev, {})
check("返回里带下载链接", "/api/doclib/download?rel=" in str(_res),
      str(_res)[:70].replace("\n", " "))
_fs = glob.glob(os.path.join(TMP, "**", "缝合回归.png"), recursive=True)
check("成品真的落盘了（生成文库）", bool(_fs), str([os.path.basename(x) for x in _fs]))
_evi = [e for e in _ev if e.get("type") == "image"]
check("回了一个图片事件给前端（带尺寸）", len(_evi) == 1 and _evi[0].get("size"),
      str(_evi[0].get("size")) if _evi else "无")
_all_png = [x for x in glob.glob(os.path.join(TMP, "**", "*.png"), recursive=True)
            if os.path.getsize(x) > 1000]
check("图片库/文库至少各留了一份可下载的成品", len(_all_png) >= 2,
      str([os.path.basename(x) for x in _all_png]))
check("来源标成 compose（前端据此标「多图缝合」，不是笼统的「图片」）",
      _evi and _evi[0].get("origin") == "compose", str(_evi[0].get("origin")) if _evi else "")

print()
print("=" * 66)
print("【5】没给 sources 时用「本轮拖进来的图」（用户说『把这几张拼起来』的常见情形）")
print("=" * 66)
_ev2 = []
_ctx = {"images": [open(p, "rb").read() for p in IMGS]}
_res2 = T.dispatch("compose_images", {"layout": "grid", "size": 320}, _ev2, _ctx)
check("自动取本轮 4 张图并拼好", "拼接完成（4 张" in str(_res2), str(_res2)[:60])
_res3 = T.dispatch("compose_images", {"layout": "grid"}, [], {"images": []})
check("一张图都没有 → 提示让用户拖图", "拖进对话" in str(_res3), str(_res3)[:70])

print()
print("=" * 66)
print("【6】★ harmonize 的幅度必须有硬上限（否则就不是「统一风格」而是「整张重画」）")
print("=" * 66)
_called = {}
_orig_edit = t2i.edit_image


def _fake_edit(img, prompt, negative_prompt="", steps=None, strength=0.6):
    _called["strength"] = strength
    _called["prompt"] = prompt
    buf = io.BytesIO()
    (img if isinstance(img, Image.Image) else Image.open(img)).save(buf, format="PNG")
    import base64
    return {"ok": True, "b64": base64.b64encode(buf.getvalue()).decode("ascii")}


t2i.edit_image = _fake_edit
try:
    _evh = []
    T.dispatch("compose_images", {"sources": IMGS[:2], "harmonize": 0.9}, _evh, {})
    check("harmonize 给 0.9 会被夹到 0.45 以内", _called.get("strength", 9) <= 0.45,
          "实际传给图生图的是 %.2f" % (_called.get("strength") or -1))
    _called.clear()
    T.dispatch("compose_images", {"sources": IMGS[:2], "harmonize": 0.3}, _evh, {})
    check("给 0.3 就按 0.3 用（不擅自放大）",
          abs((_called.get("strength") or 0) - 0.3) < 1e-6, str(_called.get("strength")))
    check("没给 prompt 时用默认的「统一光线/色调」提示",
          "consistent lighting" in (_called.get("prompt") or ""),
          (_called.get("prompt") or "")[:60])
    _called.clear()
    _evn = []
    T.dispatch("compose_images", {"sources": IMGS[:2]}, _evn, {})
    check("★ 默认 harmonize=0 → **完全不重绘**（用户没要求就别动画面）",
          "strength" not in _called, str(_called))
finally:
    t2i.edit_image = _orig_edit

print()
print("=" * 66)
print("【7】★ 不许假装能做「智能融合」（模型很爱编「已完成融合」）")
print("=" * 66)
_src_main = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
_src_tools = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
check("工具描述里写明了「不会把 A 图里的人搬到 B 图」",
      "不会" in _src_tools and "搬到" in _src_tools)
check("系统提示里也写了这条边界（否则模型看不到 schema 的模型会乱答应）",
      "智能融合做不到" in _src_main)
check("工具返回里提醒模型如实说明", "如实说明" in _src_tools)
check("compose_images 已在工具表里", any(
    s["function"]["name"] == "compose_images"
    for s in T.make_schemas(True, True, True, ask_mode="deep")))

print()
print("=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
