# -*- coding: utf-8 -*-
"""守住用户 2026-09-23 报的图片质量 / 深度询问问题。

用户原话：
  · "图片微改时容易歪曲原图"
  · "生成图片偏离用户的要求…不够满意，做工太过粗糙"
  · "要求写实的时候也不写实"
  · "也没有按用户要求改"
  · "开启深度询问时，模型没有进行多轮提问全面了解任务，单轮提问时问题数也不够多"

实测出来的**真根因**（不是"模型不行"这么笼统）：
  ① 微改**不限制底图尺寸** —— 用户拖 2400×1600 的照片进来，SD 就真在 2400×1600 上重绘
     （原生 512 的 14 倍面积）。实测：改动几乎看不出来（等于"没按要求改"），且要 44.5 秒。
  ② 所有请求都追加同一句 "professional photography, 8k…" —— 要卡通/插画时**和需求打架**。
  ③ `hd` 只是插值放大（放大不会长细节）；改成"先精修再超分"后，超分若与 SD 同时占显存
     会 OOM 退化，实测 380 秒/张（放掉 SD 后 18.6 秒）。
  ④ 深度询问只写了"个数不限、可以分多轮" —— 没有数字，模型照样只问 2~3 条。

跑法：python test_image_quality_and_ask.py   （必须用 Python 3.14）
"""
import ast
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_imgq_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import config as C              # noqa: E402
from backend import main as M                # noqa: E402
from backend import t2i                      # noqa: E402
from backend import tools as T               # noqa: E402

CFG = C.load_config()
SRC_T2I = open(os.path.join(ROOT, "backend", "t2i.py"), encoding="utf-8").read()
SRC_TOOLS = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
SRC_MAIN = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()

PASS, FAIL = 0, []


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] " + name)
    else:
        FAIL.append(name)
        print("  [!!] " + name + ("   → " + str(detail) if detail else ""))


print("=" * 68)
print("① 微改：底图分辨率必须先归一化（这是「歪曲原图」的真根因）")
print("=" * 68)
check("有 _fit_working_size 归一化函数", callable(getattr(t2i, "_fit_working_size", None)))
w, h = t2i._fit_working_size(2400, 1600)
check("2400x1600 → 长边 ≤768 且保持比例", w <= 768 and h <= 768 and abs(w / h - 1.5) < 0.02,
      "%dx%d" % (w, h))
check("所有边都是 8 的倍数（VAE 下采样 8 倍，否则会静默裁掉几像素=边缘错位）",
      w % 8 == 0 and h % 8 == 0, "%dx%d" % (w, h))
w2, h2 = t2i._fit_working_size(300, 200)
check("太小的图会被拉到下限以上（否则没细节可依）", max(w2, h2) >= t2i.EDIT_MIN_SIDE,
      "%dx%d" % (w2, h2))
w3, h3 = t2i._fit_working_size(640, 480)
check("本来就合适的尺寸不动它（640x480 原样保留）", (w3, h3) == (640, 480), "%dx%d" % (w3, h3))
check("edit_image 里真的用了它", "_fit_working_size(" in SRC_T2I
      and "work_w, work_h = _fit_working_size" in SRC_T2I)
check("改完会**还原成用户原来的尺寸**（不该顺手把人家照片改小）",
      "result = result.resize(orig_size" in SRC_T2I)
check("默认改动幅度是 0.5（不再是 0.6）",
      "arguments.get(\"strength\") or 0.5" in SRC_TOOLS)

print()
print("=" * 68)
print("② 分风格提示词：要卡通时不能再硬塞「专业摄影/8k」")
print("=" * 68)
for req, must_in, must_not in (("画一只写实的狐狸", "photorealistic", "anime"),
                               ("画一只卡通狐狸", "anime", "photorealistic, 35mm"),
                               ("来张插画风格的狐狸", "illustration", "35mm")):
    pos, neg = T._style_for(req)
    check("%s → 正向含「%s」且不含「%s」" % (req, must_in, must_not),
          must_in in pos and must_not not in pos.split(neg)[0][:120], pos[:60])
    check("%s → 有对应的负向排除" % req, bool(neg), neg[:50])
pos, neg = T._style_for("画个狐狸")
check("没说风格时用中性加成，不瞎猜", pos == "" and neg == "")
check("生图里用了 _style_for（不是写死一句通用加成）",
      "_style_for(prompt)" in SRC_TOOLS and "_style_pos" in SRC_TOOLS or "style_pos" in SRC_TOOLS)

print()
print("=" * 68)
print("③ hd：先精修再超分，且超分前要把 SD 放掉（否则 380 秒）")
print("=" * 68)
check("有 refine_detail（细节精修）", callable(getattr(t2i, "refine_detail", None)))
check("hd 路径里调用了它", "refine_detail(result, prompt, negative_prompt)" in SRC_T2I)
check("⚠️ 超分前 unload（实测同时占显存会 OOM → 380 秒）",
      "unload()\n            result, up = upscale_image(result)" in SRC_T2I)
check("交付尺寸有上限（4096 太大没必要）",
      getattr(t2i, "HD_TARGET_MAX", 0) == 2048, getattr(t2i, "HD_TARGET_MAX", None))
check("生成工具描述里的耗时说明已按实测更新（≈30 秒）", "实测 37.9s" in SRC_TOOLS)

print()
print("=" * 68)
print("④ 深度询问：要给具体数字，不能只说「不限」")
print("=" * 68)
quick = dict(CFG)
quick["ask_mode"] = "quick"
deep = dict(CFG)
deep["ask_mode"] = "deep"
line_q = M._ask_mode_line(quick)
line_d = M._ask_mode_line(deep)
check("快速模式仍只问 1~3 个", "1~3" in line_q)
check("深度模式给了**具体数量**（一轮 5~7 个）", "5~7" in line_d, line_d[:60])
check("深度模式要求**多轮**（2~3 轮）", "2~3 轮" in line_d and "再问一轮" in line_d)
check("深度模式列了要覆盖的维度（用途/受众/范围/风格/约束/交付）",
      all(k in line_d for k in ("用途", "受众", "范围", "风格", "约束", "交付")))
check("⚠️ 同时保留「问够就动手」的刹车（防退化成无限追问）", "问够就动手" in line_d)

au_deep = T._ask_user_schema("deep")["function"]["description"]
au_quick = T._ask_user_schema("quick")["function"]["description"]
check("ask_user 的**工具描述**也按模式给数量（模型先看 schema）",
      "5~7" in au_deep and "1~3" in au_quick)
check("深度模式的描述里写了要再问下一轮", "再调用本工具问下一轮" in au_deep)
sch_d = [s for s in T.make_schemas(True, True, True, ask_mode="deep")
         if s["function"]["name"] == "ask_user"][0]["function"]["description"]
check("make_schemas(ask_mode=...) 真的把模式透下去了", "5~7" in sch_d)
check("chat 接口把 ask_mode 传给了 make_schemas", "ask_mode=str(cfg.get(\"ask_mode\")" in SRC_MAIN)
check("写作轮的提问数也跟着模式走（原来写死 2~4）",
      '_ask_n = (' in SRC_MAIN and "5~7" in SRC_MAIN)
check("代码轮的 ask_user 描述也跟着模式走",
      'if "ask_user" in out and str(cfg.get("ask_mode") or "quick").lower() == "deep":' in SRC_MAIN
      and "再问一轮" in SRC_MAIN)
import json as _json   # noqa: E402
_all_txt = _json.dumps(T._ask_user_schema("quick"), ensure_ascii=False)
check("⚠️ ask_user 里仍保留「别凑数」的刹车（整份 schema 里）", "别凑数" in _all_txt)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 68)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
