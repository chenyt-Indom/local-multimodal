# -*- coding: utf-8 -*-
"""守住「抠图 + 图片合成」这条链路（2026-09-26 用户要求新增）。

背景：用户问「文生图是否支持图片之间的抠图与缝合」。当时：
  · 缝合（**排版式拼接**）已经有 —— compose_images，本测试不重复守；
  · 抠图**完全没有**（全仓库没有 rembg / U2Net / SAM 的实现，
    "抠图"只是意图识别里的一个关键词，背后没有工具）；
  · 内容级合成也没有。
本次补上两个工具：
  · cutout_image    —— 抠图（rembg + BiRefNet，**CPU 推理**，不占显存）
  · composite_image —— 内容级合成（主体**像素级保真**，不重画）

两种跑法：
  python test_cutout_composite.py          # 快跑：不加载模型，测接线/参数/落盘（几秒）
  python test_cutout_composite.py --real   # 真跑：真抠一张图 + 真合成（首次会下 ~1GB 权重）

为什么默认不真跑：BiRefNet 权重约 1GB，首次要下载、CPU 推理要几秒，
不适合当每次回归都跑的用例；但**质量必须真验证**，所以留了 --real。
"""
import io
import os
import shutil
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_cutout_")
try:
    shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
except Exception:
    pass
os.environ["MM_DATA_DIR"] = TMP
# ⚠️ 抠图模型目录**不能**指到临时目录 —— 那会让 --real 模式重新下载约 900MB。
#    用项目里已下好的那份（或命令行给的 REMBG_HOME）。
os.environ.setdefault("REMBG_HOME", os.path.join(ROOT, "rembg"))
sys.path.insert(0, ROOT)

REAL = "--real" in sys.argv

from PIL import Image, ImageDraw          # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s  %s" % (name, detail))
    else:
        FAIL += 1
        print("  [!!] %s  %s" % (name, detail))


def _fake_cutout_png(w=256, h=256):
    """造一张"抠好的"图：四周全透明，中间一个不透明圆。"""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((w // 4, h // 4, w * 3 // 4, h * 3 // 4), fill=(220, 40, 40, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _fake_remove(data, session=None, alpha_matting=False, post_process_mask=False):
    """模拟 rembg.remove —— 关键：**输出与输入同尺寸**（真实 rembg 就是这样，
    mask 会上采样回原图尺寸）。不模拟这一点，测试会误判成"尺寸不对"。"""
    src = Image.open(io.BytesIO(data))
    return _fake_cutout_png(*src.size)


def fake_rgb(path, w=400, h=300, color=(30, 60, 200)):
    Image.new("RGB", (w, h), color).save(path)
    return path


# ---------- 非 --real 模式：注入一个假 rembg，把"抠图"变成确定性输出 ----------
if not REAL:
    _m = types.ModuleType("rembg")
    _m.remove = _fake_remove
    _m.new_session = lambda name: ("fake-session", name)
    sys.modules["rembg"] = _m

from backend import cutout, composite      # noqa: E402
from backend import tools as T             # noqa: E402

# ============================================================
print("=" * 66)
print("【1】抠图模块：档位 / 入参校验 / 返回结构   (REAL=%s)" % REAL)
print("=" * 66)

check("默认档位是 birefnet-general（MIT，通用最强档之一）",
      cutout.MODEL_DEFAULT == "birefnet-general", cutout.MODEL_DEFAULT)
check("人像档位是 birefnet-portrait",
      cutout.MODEL_PORTRAIT == "birefnet-portrait", cutout.MODEL_PORTRAIT)
check("白名单含 BiRefNet 全系",
      all(m in cutout.MODEL_CHOICES for m in
          ("birefnet-general", "birefnet-portrait", "birefnet-massive",
           "birefnet-general-lite")),
      "%d 个档位" % len(cutout.MODEL_CHOICES))
check("bria-rmbg 只在白名单里、不是默认（非商业许可，不能默认用）",
      "bria-rmbg" in cutout.MODEL_CHOICES and cutout.MODEL_DEFAULT != "bria-rmbg")

ok, why = cutout.available()
check("available() 返回 (bool, 原因)", isinstance(ok, bool) and isinstance(why, str),
      "ok=%s" % ok)

r = cutout.cutout(fake_rgb(os.path.join(TMP, "a.png")), model="不存在的档位")
check("非法档位被明确拒绝（不硬塞给 rembg）",
      (not r.get("ok")) and "不认识的抠图模型" in r.get("error", ""), r.get("error", "")[:40])

r = cutout.cutout(os.path.join(TMP, "不存在.png"))
check("找不到的图片给出清楚报错",
      (not r.get("ok")) and ("找不到图片" in r.get("error", "")), r.get("error", "")[:40])

r = cutout.cutout(fake_rgb(os.path.join(TMP, "b.png")))
check("正常抠图返回 ok", r.get("ok") is True, str(r.get("error", ""))[:60])
if r.get("ok"):
    check("返回带透明通道（has_alpha）", r.get("has_alpha") is True)
    check("返回 PNG 字节", isinstance(r.get("png"), (bytes, bytearray)) and len(r["png"]) > 100,
          "%d 字节" % len(r["png"]))
    check("返回尺寸正确", (r.get("width"), r.get("height")) == (400, 300),
          "%sx%s" % (r.get("width"), r.get("height")))
    check("返回里带了所用档位", r.get("model") == "birefnet-general", r.get("model"))

# ============================================================
print()
print("=" * 66)
print("【2】合成模块：位置 / 缩放 / 背景 / 像素保真")
print("=" * 66)

subj_png = os.path.join(TMP, "subj.png")
with open(subj_png, "wb") as f:
    f.write(_fake_cutout_png(256, 256))
bg_path = fake_rgb(os.path.join(TMP, "bg.png"), 400, 300, (10, 20, 30))

r = composite.composite(subj_png, bg_path, position="center")
check("居中合成 ok", r.get("ok") is True, str(r.get("error", ""))[:60])
if r.get("ok"):
    check("输出沿用背景尺寸", (r["width"], r["height"]) == (400, 300),
          "%dx%d" % (r["width"], r["height"]))
    check("居中位置 = ((400-256)/2, (300-256)/2) = (72, 22)",
          r.get("position") == (72, 22), str(r.get("position")))

r = composite.composite(subj_png, bg_path, position="top-left", margin=10)
check("左上 + 留白 10 → (10, 10)",
      r.get("ok") and r.get("position") == (10, 10), str(r.get("position")))

r = composite.composite(subj_png, bg_path, position="bottom-right", margin=10)
check("右下 + 留白 10 → (134, 34)",
      r.get("ok") and r.get("position") == (134, 34), str(r.get("position")))

r = composite.composite(subj_png, bg_path, position="100,50")
check("支持 “x,y” 像素坐标",
      r.get("ok") and r.get("position") == (100, 50), str(r.get("position")))

r = composite.composite(subj_png, bg_path, position="胡说八道")
check("非法位置被明确拒绝", (not r.get("ok")), r.get("error", "")[:40])

r = composite.composite(subj_png, bg_path, scale=0.5)
check("缩放 0.5 → 主体 128×128，位置 (136, 86)",
      r.get("ok") and r.get("subject_size") == (128, 128) and r.get("position") == (136, 86),
      "%s / %s" % (r.get("subject_size"), r.get("position")))

r = composite.composite(subj_png, "FF0000", canvas="200x200")
check("纯色背景（纯色 + 画布尺寸）",
      r.get("ok") and (r["width"], r["height"]) == (200, 200),
      "%sx%s" % (r.get("width"), r.get("height")))

r = composite.composite(subj_png, "not-a-color-and-not-a-file")
check("背景既不是颜色也不是文件时报错清楚",
      (not r.get("ok")) and ("背景读取失败" in r.get("error", "")), r.get("error", "")[:50])

# 像素保真：主体颜色必须**原样**出现在输出里（没有被任何模型重画）
r = composite.composite(subj_png, "FFFFFF", canvas="256x256", position="center")
if r.get("ok"):
    out = Image.open(io.BytesIO(r["png"])).convert("RGB")
    center = out.getpixel((128, 128))
    corner = out.getpixel((2, 2))
    check("主体像素原样保留（中心是主体的红 220,40,40）",
          center == (220, 40, 40), str(center))
    check("背景像素原样保留（角落是白底）", corner == (255, 255, 255), str(corner))
else:
    check("像素保真", False, r.get("error", ""))

# 没有透明通道 + 关掉 auto_cutout → 必须老实报错，不能硬贴
rgb_subj = fake_rgb(os.path.join(TMP, "rgb.png"), 100, 100, (5, 5, 5))
r = composite.composite(rgb_subj, bg_path, auto_cutout=False)
check("主体无 alpha 且关掉自动抠图 → 明确要求先抠",
      (not r.get("ok")) and ("没有透明通道" in r.get("error", "")), r.get("error", "")[:50])

# 没有透明通道 + 开 auto_cutout → 自动抠完再合成
r = composite.composite(rgb_subj, bg_path, auto_cutout=True)
check("主体无 alpha 且开启自动抠图 → 自动抠后合成成功",
      r.get("ok") and r.get("cutout") is True, str(r.get("error", ""))[:50])

# ---------- shrink（去白边）----------
# ⚠️ 为什么必须守：抠出来的主体，边缘那圈像素是**混了原背景色**的半透明像素，
#    浅底抠的人贴到深背景上会浮出一圈白色光晕（实测长卷发贴海滩非常明显）。
#    shrink 把 alpha 向内收几像素来切掉它 —— 这条断言确认它**真的**收进去了。
r0 = composite.composite(subj_png, bg_path, shrink=0)
r2 = composite.composite(subj_png, bg_path, shrink=2)
if r0.get("ok") and r2.get("ok"):
    # ⚠️ 不能量合成图的 alpha —— 背景是实底的，整张 alpha 都是 255。
    #    要量**主体本身**：数一数"主体色"的像素个数。
    def _subject_pixels(png):
        im = Image.open(io.BytesIO(png)).convert("RGB")
        return sum(1 for p in im.getdata() if p == (220, 40, 40))

    n0, n2 = _subject_pixels(r0["png"]), _subject_pixels(r2["png"])
    check("★ shrink 真的把主体边缘收进去了（去白边生效）",
          n2 < n0, "主体像素 %d → %d（收了 %d）" % (n0, n2, n0 - n2))

    # 直接对函数测：alpha 的非透明像素数必须下降
    _im = Image.open(subj_png).convert("RGBA")
    _c0 = sum(composite._shrink_alpha(_im, 0).getchannel("A").histogram()[1:])
    _c2 = sum(composite._shrink_alpha(_im, 2).getchannel("A").histogram()[1:])
    check("_shrink_alpha 直接调用也生效（腐蚀掉最外圈）",
          _c2 < _c0, "非透明像素 %d → %d" % (_c0, _c2))

    _sig = __import__("inspect").signature(composite.composite)
    check("shrink 默认值是 2（实测最优档：切掉白边又不啃发丝）",
          _sig.parameters["shrink"].default == 2,
          "默认 %s" % _sig.parameters["shrink"].default)
else:
    check("shrink 对比", False, str(r0.get("error") or r2.get("error"))[:50])

# ============================================================
print()
print("=" * 66)
print("【3】工具接线：schema / dispatch / 端到端（走 tools 层）")
print("=" * 66)

names = []
_schemas_list = T.make_schemas()
for tool in _schemas_list:
    fn = tool.get("function") or {}
    names.append(fn.get("name"))
check("cutout_image 已注册进工具表", "cutout_image" in names)
check("composite_image 已注册进工具表", "composite_image" in names)

_schemas = {t["function"]["name"]: t["function"] for t in _schemas_list
            if "function" in t}
_cut = _schemas.get("cutout_image", {})
_comp = _schemas.get("composite_image", {})
check("cutout_image 的描述里点明了「抠图/去背景」触发词",
      "抠图" in _cut.get("description", "") and "去背景" in _cut.get("description", ""))
check("composite_image 的描述里点明了与 compose_images 的分工",
      "compose_images" in _comp.get("description", ""))
check("composite_image 必填 subject + background",
      set(_comp.get("parameters", {}).get("required", [])) == {"subject", "background"},
      str(_comp.get("parameters", {}).get("required")))

# 端到端（tools 层）：抠图
msg = T.dispatch("cutout_image", {"source": subj_png}, [], {})
check("tools 层抠图返回了成功文案", "抠图完成" in msg, msg.replace("\n", " ")[:70])

# 端到端：合成
msg = T.dispatch("composite_image", {"subject": subj_png, "background": "FFFFFF",
                                     "canvas": "256x256"}, [], {})
check("tools 层合成返回了成功文案", "合成完成" in msg, msg.replace("\n", " ")[:70])
check("tools 层合成提示了「像素级真实合成」",
      "像素级真实合成" in msg, msg.replace("\n", " ")[:70])

# 缺参要报清楚
msg = T.dispatch("composite_image", {"subject": subj_png}, [], {})
check("缺 background 时报错清楚", "缺少 background" in msg, msg[:60])

msg = T.dispatch("cutout_image", {}, [], {})
check("既没 source 又没拖图时报错清楚", "没有指定要抠的图" in msg, msg[:60])

# 拖进来的图能兜底（context.images）
msg = T.dispatch("cutout_image", {}, [], {"images": [subj_png]})
check("可以用本轮拖进对话的图兜底", "抠图完成" in msg, msg.replace("\n", " ")[:60])

# ============================================================
print()
print("=" * 66)
print("【4】模型下载：镜像回退 + md5 校验 + 落盘名（全程 mock，不真下）")
print("=" * 66)

# 为什么单测这块：rembg 自己下载走 GitHub 直连，国内必然
# ConnectionResetError(10054)。我们自己实现了一套"镜像优先"的下载，
# 它要是错了，用户换档位就会卡死 —— 必须守住。
import hashlib as _hashlib                      # noqa: E402
import urllib.error                             # noqa: E402

_ORIG_DL = cutout._download
_ORIG_MD5 = cutout._md5_of
_ORIG_ENV = os.environ.get("REMBG_HOME")

# 造一个"假权重"：内容固定，这样能算出真 md5
_FAKE_BYTES = b"FAKE-ONNX-WEIGHT" * 80000      # ≈1.4MB，过 size 门槛
_FAKE_MD5 = _hashlib.md5(_FAKE_BYTES).hexdigest()


def _reset_env(home_dir):
    os.environ["REMBG_HOME"] = home_dir
    shutil.rmtree(home_dir, ignore_errors=True)
    os.makedirs(home_dir, exist_ok=True)


# --- 4.1 正常路径：第一个镜像就成功 ---
_reset_env(os.path.join(TMP, "rb1"))
_tried = []


def _dl_ok(url, dest, timeout=30):
    _tried.append(url)
    with open(dest, "wb") as f:
        f.write(_FAKE_BYTES)


cutout._download = _dl_ok
cutout._md5_of = lambda p: _FAKE_MD5          # 假装校验通过（内容是假的）
cutout._MODEL_FILES["birefnet-general"] = ("BiRefNet-general-epoch_244.onnx", _FAKE_MD5)
ok, why = cutout.ensure_model("birefnet-general")
check("首次下载成功", ok is True, why[:60])
check("走的是第一个镜像（gh-proxy.com，实测最快）",
      _tried and _tried[0].startswith("https://gh-proxy.com/"), _tried[0][:50] if _tried else "")
check("★ 落盘名是 <档位>.onnx（不是 release 上的原名）",
      os.path.isfile(os.path.join(TMP, "rb1", "models", "birefnet-general",
                                  "birefnet-general.onnx")))
_n_first = len(_tried)
cutout.ensure_model("birefnet-general")
check("下完后再调 ensure_model 不再重复下载",
      len(_tried) == _n_first, "多发了 %d 次请求" % (len(_tried) - _n_first))

# --- 4.2 已存在时直接返回，不发请求 ---
_n_before = len(_tried)
ok2, _ = cutout.ensure_model("birefnet-general")
check("已有权重时短路返回、零请求",
      ok2 is True and len(_tried) == _n_before, "%d 次请求" % (len(_tried) - _n_before))

# --- 4.3 第一个镜像失败 → 自动退到下一个 ---
cutout._md5_of = _ORIG_MD5          # 用真实 md5（_FAKE_MD5 本来就是真算出来的，能匹配）
_reset_env(os.path.join(TMP, "rb2"))
_seq = []


def _dl_flaky(url, dest, timeout=30):
    _seq.append(url)
    if "gh-proxy.com" in url:
        raise OSError("模拟：远程主机强迫关闭了一个现有的连接")
    with open(dest, "wb") as f:
        f.write(_FAKE_BYTES)


cutout._download = _dl_flaky
ok3, why3 = cutout.ensure_model("birefnet-general")
check("镜像失败会自动回退到下一个镜像",
      ok3 is True and any("gh-proxy.com" in u for u in _seq)
      and any("ghfast.top" in u for u in _seq),
      "试了 %d 个" % len(_seq))

# --- 4.4 md5 不匹配 → 不能留坏文件，要换镜像重试 ---
# ⚠️ 这一步必须先把 _md5_of 换回**真实实现** —— 4.1 里为了造 md5 把它 mock 掉了，
#    不还原的话"不匹配"永远测不出来（会一路判成通过）。
cutout._md5_of = _ORIG_MD5
_reset_env(os.path.join(TMP, "rb3"))
_seq4 = []


def _dl_bad_md5(url, dest, timeout=30):
    _seq4.append(url)
    with open(dest, "wb") as f:
        f.write(b"BROKEN" * 300000)           # 内容错 → md5 不匹配


cutout._download = _dl_bad_md5
ok4, why4 = cutout.ensure_model("birefnet-general")
check("md5 校验不过时判定为失败", ok4 is False, why4[:60])
check("校验不过会把坏文件删掉（不留 .onnx 骗 rembg）",
      not os.path.isfile(os.path.join(TMP, "rb3", "models", "birefnet-general",
                                      "birefnet-general.onnx")))
check("会把所有镜像都试一遍", len(_seq4) >= 3, "试了 %d 个" % len(_seq4))

# --- 4.5 全部失败 → 报错要说人话，并给出手动下载办法 ---
check("失败信息里有「已试过国内镜像」和手动下载指引",
      "镜像" in why4 and "手动下载" in why4, why4.replace("\n", " ")[:70])

# --- 4.6 表里没有的档位不阻断（交回 rembg 自己处理） ---
ok6, _ = cutout.ensure_model("isnet-anime")
check("白名单内、但下载表里没有的档位不阻断（交回 rembg）", ok6 is True)

# 还原（⚠️ 连 _MODEL_FILES 也要还原 —— 上面为了造 md5 改过它，
#       不还原的话会污染后面 --real 的真实校验）
cutout._download = _ORIG_DL
cutout._md5_of = _ORIG_MD5
cutout._MODEL_FILES["birefnet-general"] = ("BiRefNet-general-epoch_244.onnx",
                                           "7a35a0141cbbc80de11d9c9a28f52697")
if _ORIG_ENV:
    os.environ["REMBG_HOME"] = _ORIG_ENV

# ============================================================
print()
print("=" * 66)
print("【5】真实模型验证（只在 --real 下跑）")
print("=" * 66)
if REAL:
    print("  （首次会下载 ~1GB 权重，请耐心）")
    # 造一张白底 + 深色圆的图，抠完中心应该不透明、四角应该透明
    src = os.path.join(TMP, "real_src.png")
    im = Image.new("RGB", (512, 512), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.ellipse((106, 106, 406, 406), fill=(30, 90, 200))
    im.save(src)

    rr = cutout.cutout(src)
    check("真实抠图成功", rr.get("ok") is True, str(rr.get("error", ""))[:80])
    if rr.get("ok"):
        out = Image.open(io.BytesIO(rr["png"])).convert("RGBA")
        a = out.getchannel("A")
        c = a.getpixel((256, 256))          # 圆心：主体 → 应该不透明
        k = a.getpixel((6, 6))              # 角落：背景 → 应该透明
        check("圆心 alpha 接近不透明", c > 200, "alpha=%d" % c)
        check("角落 alpha 接近透明", k < 60, "alpha=%d" % k)
        check("模型档位正确", rr.get("model") == "birefnet-general", rr.get("model"))

        # 真合成
        rc = composite.composite(rr["png"], "F2F2F2", canvas="512x512", shadow=True)
        check("真实抠图结果能直接合成", rc.get("ok") is True, str(rc.get("error", ""))[:80])
else:
    print("  （跳过；要真验证质量请加 --real）")

print()
print("=" * 66)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
