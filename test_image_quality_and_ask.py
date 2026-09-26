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
# ⚠️ 工作分辨率的**上限随模型代际变**：SD2.1 是 768，SDXL 是 1024
#    （SDXL 原生就是 1024，按 768 重绘等于喂了张偏小的图 → 糊 + 结构飘）。
#    所以这里别写死 768，跟着 t2i 的判定走，换底模不用改测试。
_want_max = 1024 if t2i.is_xl() else t2i.EDIT_MAX_SIDE
check("2400x1600 → 长边 ≤%d 且保持比例" % _want_max,
      w <= _want_max and h <= _want_max and abs(w / h - 1.5) < 0.02,
      "%dx%d（上限 %d）" % (w, h, _want_max))
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

print()
print("=" * 68)
print("⑤ 深度询问的**代码层兜底**（光靠提示词不可靠，实测两次结果不一致）")
print("=" * 68)
check("有 ASK_DEEP_MIN 常量且 >1", getattr(T, "ASK_DEEP_MIN", 0) > 1,
      getattr(T, "ASK_DEEP_MIN", None))
check("有 ask_guard_reset（每轮清零）", callable(getattr(T, "ask_guard_reset", None)))
check("chat 接口每轮会重置守卫", "tools.ask_guard_reset(session)" in SRC_MAIN)


class _FakeUI:
    """假的前端通道：把弹框调用记下来。"""

    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        return [{"question": q["question"], "answer": "随便，你定"}
                for q in (payload.get("questions") or [])]


def ask(n, mode):
    ui = _FakeUI()
    T.ask_guard_reset("t-" + mode + str(n))
    out = T._do_ask_user({"questions": [{"question": "问题%d？" % i} for i in range(1, n + 1)]},
                         {"ask": ui, "ask_mode": mode, "session": "t-" + mode + str(n)})
    return ui, out


ui, out = ask(3, "deep")
check("深度模式只问 3 个 → **不弹框**，驳回并要求补全", not ui.calls and "至少" in out,
      out[:80])
check("驳回文案里给了具体数量（至少 N 个 / 5~7 个）", "5~7" in out and "至少" in out)
ui, out = ask(5, "deep")
check("深度模式第一轮问 5 个 → 正常弹框", len(ui.calls) == 1 and not out.startswith("⚠️"),
      "%d 次弹框" % len(ui.calls))

# ⚠️⚠️ 关键：**下限只管第一轮**。第二轮是"追问"，问 2 个才正常 ——
# 第一版没加这个条件时，模型第二轮想追问 2 个被驳回，那一轮**根本没弹出来**（越修越糟）。
T.ask_guard_reset("t-round2")
# ⚠️ 两次都要用**同一个 session**（ask() 辅助函数会自己重置守卫，不能用它）
_ui1 = _FakeUI()
T._do_ask_user({"questions": [{"question": "第一轮问题%d？" % i} for i in range(5)]},
               {"ask": _ui1, "ask_mode": "deep", "session": "t-round2"})
ui2 = _FakeUI()
out2 = T._do_ask_user({"questions": [{"question": "追问1？"}, {"question": "追问2？"}]},
                      {"ask": ui2, "ask_mode": "deep", "session": "t-round2"})
check("⚠️ 第二轮**追问**只问 2 个 → 照样弹框（不能被下限拦住）",
      len(ui2.calls) == 1 and not out2.startswith("⚠️"),
      "%d 次弹框 / %s" % (len(ui2.calls), out2[:40]))
check("⚠️ 弹框之后，工具返回值里有「该不该再问一轮」的自查提示",
      "请先自查再动手" in out, out[-90:].replace("\n", " "))
ui, out = ask(2, "quick")
check("快速模式问 2 个 → 照样弹框（不设下限，别打扰）", len(ui.calls) == 1)

# 驳回最多 2 次，之后必须放行（否则会死循环）
T.ask_guard_reset("t-cap")
outs = []
for _ in range(3):
    ui = _FakeUI()
    outs.append(T._do_ask_user(
        {"questions": [{"question": "只问一个？"}]},
        {"ask": ui, "ask_mode": "deep", "session": "t-cap"}))
check("⚠️ 连续驳回最多 2 次，第 3 次必须放行（不会死循环）",
      outs[0].startswith("⚠️") and outs[1].startswith("⚠️") and not outs[2].startswith("⚠️"),
      [o[:12] for o in outs])
check("下限常量与目标一致（5）", getattr(T, "ASK_DEEP_MIN", 0) == 5,
      getattr(T, "ASK_DEEP_MIN", None))

# 问到第 3 轮时要催它动手
T.ask_guard_reset("t-rounds")
# ⚠️ 每轮问**不同的话题**，否则会被复问去重拦下（那条是另一条断言在管的）
_round_axes = (("用途", "受众", "篇幅", "风格", "约束"),
               ("交付", "节奏", "口吻", "案例", "禁忌"),
               ("配图", "尺寸", "语言", "版本", "落款"))
for k in range(3):
    ui = _FakeUI()
    last = T._do_ask_user(
        {"questions": [{"question": "%s方面还有别的要求吗%d？" % (ax, k)}
                       for ax in _round_axes[k]]},
        {"ask": ui, "ask_mode": "deep", "session": "t-rounds"})
check("⚠️ 问到第 3 轮时提示「现在就动手做」", "现在就动手做" in last, last[-70:])

print()
print("=" * 68)
print("⑥ 复问去重（第二轮不许把已经答过的方向再问一遍）")
print("=" * 68)
check("有 _q_similar / _q_topic", callable(getattr(T, "_q_similar", None))
      and callable(getattr(T, "_q_topic", None)))
# 判据用**真机上第二轮的原话**校准
_pairs = [("希望控制在几页左右？", "希望控制多少页？", True),
          ("这份PPT主要用于什么场合？", "这份PPT主要为哪些人准备的？", False),
          ("这份PPT是用于什么场景呢？", "这份PPT是用于什么场景？", True),
          ("有没有特别要强调的内容？", "偏向什么色彩搭配？", False),
          ("受众是谁？", "要多少页？", False),
          ("您倾向哪种风格？", "偏爱什么样的色彩搭配？", True)]
_okn = sum(1 for a, b, e in _pairs if T._q_similar(a, b) == e)
check("判重判据在 6 组真机样本上全对（换词同问要判重、不同问题不能误判）",
      _okn == len(_pairs), "%d/%d" % (_okn, len(_pairs)))

# 行为：第二轮整批复问 → 驳回
T.ask_guard_reset("t-dup")
_ui = _FakeUI()
T._do_ask_user({"questions": [{"question": "这份PPT用于什么场合？"},
                              {"question": "希望控制在几页？"},
                              {"question": "倾向哪种风格？"},
                              {"question": "面向哪些人？"},
                              {"question": "有什么必须包含的？"}]},
               {"ask": _ui, "ask_mode": "deep", "session": "t-dup"})
_ui2 = _FakeUI()
_out2 = T._do_ask_user({"questions": [{"question": "这份PPT主要用于什么场合呢？"},
                                      {"question": "大约要多少页？"},
                                      {"question": "偏爱什么色彩搭配？"}]},
                       {"ask": _ui2, "ask_mode": "deep", "session": "t-dup"})
check("⚠️ 第二轮整批复问 → **不弹框**、驳回并要求只问空白",
      not _ui2.calls and "重复" in _out2, _out2[:70])

# 行为：第二轮问**新方向** → 放行
T.ask_guard_reset("t-new")
_ui3 = _FakeUI()
T._do_ask_user({"questions": [{"question": "这份PPT用于什么场合？"},
                              {"question": "希望控制在几页？"},
                              {"question": "倾向哪种风格？"},
                              {"question": "面向哪些人？"},
                              {"question": "有什么必须包含的？"}]},
               {"ask": _ui3, "ask_mode": "deep", "session": "t-new"})
_ui4 = _FakeUI()
_out4 = T._do_ask_user({"questions": [{"question": "需要配图吗？要实景照片还是示意图？"}]},
                       {"ask": _ui4, "ask_mode": "deep", "session": "t-new"})
check("第二轮问**新方向**（配图）→ 正常弹框", len(_ui4.calls) == 1, _out4[:60])
check("⚠️ 去重驳回**最多一次**（第二次必须放行，别卡死）",
      "重复" in _out2 and "重复" not in _out4)

# ============================================================
#  2026-09-26 补：两个实测挖出来的真问题，必须长期防住
# ============================================================
print()
print("=" * 68)
print("⑨ ★ 画图前必须把对话模型请出显存（否则单张图慢 20 倍）")
# 实测：12GB 卡上 qwen3-vl:8b 常驻 5.79GB（keep_alive 30 分钟一直在），
# 而 SDXL 要 11.5GB —— 两者不可能共存。共存时 SDXL 被挤进共享内存：
#   同一张 1024²/fp16/28 步：**有 Qwen 占着 125~189 秒**，释放后 **6 秒**。
# 这条断言防的就是"以后有人把 free_ollama_vram 删了/忘了接"。
_src_t2i = open(os.path.join(ROOT, "backend", "t2i.py"), encoding="utf-8").read()
check("t2i 里有 free_ollama_vram()", "def free_ollama_vram" in _src_t2i)
check("文生图加载前会调用它", _src_t2i.count("free_ollama_vram()") >= 3,
      "共 %d 处（定义 1 + 文生图 1 + 图生图 1）" % _src_t2i.count("free_ollama_vram()"))
check("它按 keep_alive=0 让 Ollama 卸载", '"keep_alive": 0' in _src_t2i)
check("卸载失败不会把画图搞挂（有 except 兜底）",
      _src_t2i.count("pass                      # Ollama 没开 / 模型没装") >= 1)

print()
print("⑩ ★ 不许再对「微改能精确改元素」做没验证过的承诺")
# 实测（同一张写实人像，逐档试「换衣服颜色」「加眼镜」）：
#   0.45 / 0.60 / 0.85 **全都没改出来**；0.85 试「换背景」两次结果一次换成了别的场景、
#   一次压根没换。所以描述里必须写"做不到精确改动"，而不是拍脑袋列一个"0.35~0.45=换颜色"。
_src_tools = open(os.path.join(ROOT, "backend", "tools.py"), encoding="utf-8").read()
check("edit_image 的描述写明「做不到精确改动某个元素」",
      "做不到" in _src_tools and "精确改动" in _src_tools)
check("不再承诺「0.35~0.45 = 换颜色 / 加小物件」",
      "0.35~0.45 = 换颜色" not in _src_tools)
check("写明了会如实告诉用户、不假装改到了", "别假装改到了" in _src_tools)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 68)
print("通过 %d 项，失败 %d 项" % (PASS, len(FAIL)))
for f in FAIL:
    print("   !! " + f)
sys.exit(1 if FAIL else 0)
