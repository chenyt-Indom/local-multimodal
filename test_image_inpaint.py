# -*- coding: utf-8 -*-
"""守住「局部重绘（精确微改）」这条链路（2026-09-26 用户批准新增）。

背景（实测，决定了为什么必须做它）：
  纯图生图（SDXL base，无蒙版）**做不到精确改某个元素** ——
    「加眼镜」0.45 / 0.85 → 都没戴上；「换衣服颜色」0.60 / 0.85 → 颜色都没变；
    「换背景」0.85 两次 → 一次换成别的场景、一次压根没换（只把脸重画了）。
  所以补了 inpainting + 蒙版（白=要改）：圈外像素原样保留，圈内才重画。

本测试**不加载模型**（几秒跑完）：把 t2i.inpaint / inpaint_ready 换成假的，
专测"该走重绘时必须走、参数传对了没、没装模型时话说明白没"，外加前端接线是否还在。

跑法：python test_image_inpaint.py   （纯 CPU）
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_inpaint_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import t2i                    # noqa: E402
from backend import tools as T             # noqa: E402
from PIL import Image                      # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s  %s" % (name, detail))
    else:
        FAIL += 1
        print("  [!!] %s  %s" % (name, detail))


def ratio_of(mask):
    h = mask.histogram()
    return sum(i * c for i, c in enumerate(h)) / (255.0 * max(1, mask.size[0] * mask.size[1]))


def fake_png(w=512, h=512, color=(180, 180, 180)):
    p = os.path.join(TMP, "src%d.png" % (abs(hash((w, h, color))) % 100000))
    Image.new("RGB", (w, h), color).save(p)
    return p


print("=" * 66)
print("【1】蒙版构造：区域 / 预设 / 羽化")
print("=" * 66)
m = t2i.mask_from_regions((1000, 1000), [[0.2, 0.4, 0.8, 0.85]])
r = ratio_of(m)
check("归一化区域能造出蒙版（torso 约占 0.2~0.35）", 0.18 < r < 0.36, "占比 %.2f" % r)
check("白=要改（区域中心是白的）", m.getpixel((500, 600)) > 200, m.getpixel((500, 600)))
check("黑=保留（左上角没被圈到）", m.getpixel((5, 5)) < 30, m.getpixel((5, 5)))
m2 = t2i.mask_from_regions((1000, 1000), [[200, 400, 800, 850]])   # 像素坐标
check("像素坐标也认（与归一化等价）", abs(ratio_of(m2) - r) < 0.03,
      "%.2f vs %.2f" % (ratio_of(m2), r))
check("多个区域会叠加", ratio_of(t2i.mask_from_regions(
    (1000, 1000), [[0, 0, .5, .5], [.5, .5, 1, 1]])) > ratio_of(
    t2i.mask_from_regions((1000, 1000), [[0, 0, .5, .5]])))
soft = t2i.mask_from_regions((1000, 1000), [[.25, .25, .75, .75]], feather=20)
hard = t2i.mask_from_regions((1000, 1000), [[.25, .25, .75, .75]], feather=0)
mid = soft.getpixel((250, 500))
check("边沿做了羽化（不会留生硬接缝）", 0 < mid < 255, "边缘灰度 %d" % mid)
check("不羽化时边缘是硬的（对照）", hard.getpixel((240, 500)) < 30,
      hard.getpixel((240, 500)))
for name in ("face", "torso", "lower-third", "center", "whole", "upper"):
    check("区域预设 %s 可用" % name, t2i.mask_from_area((512, 512), name) is not None)
check("认不出的区域名 → 返回 None（让上层报错，而不是默默整图重绘）",
      t2i.mask_from_area((512, 512), "乱写的") is None)

print()
print("=" * 66)
print("【2】装好了没：必须真的看到权重文件（下载中途不能算装好）")
print("=" * 66)
d = tempfile.mkdtemp(prefix="fake_inpaint_")
os.environ["SD_INPAINT_DIR"] = d
json.dump({}, open(os.path.join(d, "model_index.json"), "w"))
check("只有 model_index.json → **不算装好**", t2i.inpaint_ready() is False)
for sub in ("unet", "text_encoder", "vae"):
    os.makedirs(os.path.join(d, sub), exist_ok=True)
open(os.path.join(d, "unet", "diffusion_pytorch_model.fp16.safetensors"), "w").close()
open(os.path.join(d, "text_encoder", "model.fp16.safetensors"), "w").close()
open(os.path.join(d, "vae", "diffusion_pytorch_model.fp16.safetensors"), "w").close()
check("权重齐了 → 算装好", t2i.inpaint_ready() is True)
open(os.path.join(d, "unet", "x.safetensors.incomplete"), "w").close()
check("还有 .incomplete 残留 → 仍不算装好（避免加载到一半报错）",
      t2i.inpaint_ready() is False)
os.remove(os.path.join(d, "unet", "x.safetensors.incomplete"))
os.environ.pop("SD_INPAINT_DIR", None)

print()
print("=" * 66)
print("【3】工具路径：给了区域就必须走重绘（而不是退回整图 img2img）")
print("=" * 66)
called = {}
_orig_inpaint, _orig_ready = t2i.inpaint, t2i.inpaint_ready
try:
    t2i.inpaint_ready = lambda: True

    def _fake_inpaint(img, prompt, mask=None, regions=None, area="",
                      negative_prompt="", strength=0.85, steps=None, feather=12):
        called.update(prompt=prompt, mask=mask, regions=regions, area=area,
                      strength=strength, steps=steps, negative=negative_prompt)
        return {"ok": True, "b64": "ZmFrZQ==", "device": "cuda",
                "model": "inpaint-fake", "mask_ratio": 0.28, "size_note": ""}

    t2i.inpaint = _fake_inpaint
    ev = []
    out = T.dispatch("edit_image", {"prompt": "change the lab coat to navy blue",
                                    "regions": [[0.2, 0.4, 0.8, 0.85]],
                                    "source": fake_png()}, ev, {})
    check("★ 给了 regions → 调的是 t2i.inpaint（不是 img2img）", bool(called),
          str(list(called))[:60])
    check("区域原样传进去了", called.get("regions") == [[0.2, 0.4, 0.8, 0.85]],
          str(called.get("regions")))
    check("重绘的幅度按 inpainting 的档（≥0.75，不然改不动）",
          (called.get("strength") or 0) >= 0.75, str(called.get("strength")))
    check("提示里补了「圈外保持不变」的约束",
          "unchanged" in (called.get("prompt") or ""), (called.get("prompt") or "")[:60])
    check("返回里说清是「指定区域」重绘", "指定区域" in str(out), str(out)[:70])
    check("给前端标的是微改（origin=edit）",
          ev and ev[0].get("origin") == "edit", str(ev[0].get("origin")) if ev else "")

    called.clear()
    ev2 = []
    T.dispatch("edit_image", {"prompt": "give her glasses", "area": "face",
                              "source": fake_png()}, ev2, {})
    check("用 area 预设也走重绘", called.get("area") == "face", str(called.get("area")))

    called.clear()
    ev3 = []
    # ⚠️ 上下文里的 images 是**base64 字符串**（和真实请求一致）——
    #    我第一版塞了原始 PNG 字节，handler 去 base64 解码就失败、提前返回
    #    "无法确定要修改的图片"，于是这条断言假失败。真实应用传的是字符串。
    _src_b64 = __import__("base64").b64encode(open(fake_png(), "rb").read()).decode()
    ctx = {"images": [_src_b64], "mask": "ZmFrZU1hc2s="}
    T.dispatch("edit_image", {"prompt": "change the shirt color"}, ev3, ctx)
    check("★ 用户涂抹的蒙版（在上下文里）会被自动用上",
          called.get("mask") == "ZmFrZU1hc2s=", str(called.get("mask"))[:30])

    called.clear()
    ev4 = []
    T.dispatch("edit_image", {"prompt": "make it warmer", "source": fake_png()}, ev4, {})
    check("没给区域 → **不**走重绘（退回整图 img2img，且不再多花时间）",
          not called, str(called)[:60])

    # 没装模型时：要明确说清楚，而不是悄悄退回整图（那会让用户以为改到了）
    t2i.inpaint_ready = lambda: False
    out2 = T.dispatch("edit_image", {"prompt": "换蓝", "regions": [[.2, .4, .8, .8]],
                                      "source": fake_png()}, [], {})
    check("★ 没装重绘模型 → 明确报「需要局部重绘模型」，不假装成功",
          "局部重绘" in str(out2) and "还没装" in str(out2), str(out2)[:80])
finally:
    t2i.inpaint, t2i.inpaint_ready = _orig_inpaint, _orig_ready

print()
print("=" * 66)
print("【4】前后端接线都在（防「改了后端忘了前端」）")
print("=" * 66)
_src_main = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
_src_tools = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
_src_t2i = open(os.path.join(ROOT, "backend", "t2i.py"), encoding="utf-8").read()
_html = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
_js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
check("请求体支持 mask_b64", "mask_b64" in _src_main)
check("蒙版进了工具上下文", '"mask": (list(req.mask_b64' in _src_main)
check("工具 schema 里有 mask / regions / area 三个参数",
      all(('"%s": {' % k) in _src_tools for k in ("mask", "regions", "area")))
check("系统提示教了「要精确改就给区域」", "精确微改" in _src_main and "regions" in _src_main)
check("t2i 里有 inpaint / 蒙版构造 / 区域预设",
      all(x in _src_t2i for x in ("def inpaint(", "def mask_from_regions",
                                  "AREA_PRESETS", "def inpaint_ready")))
check("前端有选区条（index.html）", 'id="maskChip"' in _html)
check("前端有涂抹编辑器与发送接线（app.js）",
      all(x in _js for x in ("openMaskEditor", "mask_b64: selMask",
                             "涂抹选区", "buildMask")))
check("图片卡片上有「涂抹选区」入口", "mask-pick" in _js)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
