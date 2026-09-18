# -*- coding: utf-8 -*-
"""验证"长期记忆提炼"的取舍口径：该记的记、不该记的不记。

为什么要单独测这个：长期记忆是**跨对话**的档案，一旦写进垃圾（一次性查询的
评分/天气、本应用的操作说明），它会一直占着位置、还会影响后面所有对话的判断。
而这件事没法靠"跑一遍应用看看"验证 —— 提炼是后台跑的、有防抖、还慢。

做法：直接构造一段**合成对话**（里面故意同时放"垃圾"和"该记的用户信息"），
把提炼提示词喂给模型，看它到底挑出什么。
⚠️ 全程**不碰真实记忆**（临时 MM_DATA_DIR），所以可以放心造素材。

跑法：python test_memory_extract.py
（用应用自己的解释器跑，保证和线上一致）
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_mem_test_")
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import config, main as M      # noqa: E402

FAIL = []


def check(label, cond, extra=""):
    print(("  [OK] " if cond else "  [!!] ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(label)


# ---------------------------------------------------------------- 合成对话
# 故意混了四类内容：
#   A 一次性查询的结果（**不该记**）—— 餐厅评分
#   B 本应用的操作说明（**不该记**）—— 某功能怎么用
#   C 关于用户的人与未来意图（**该记**）—— 妹妹、寒假打算
#   D 用户偏好（**该记**）—— 回答格式
CONVO = "\n".join([
    "用户：汕头大学附近有什么好吃的？要评分高的，我想走路过去",
    "助手：附近查到 3 家：第二、三饭堂（199 米）、西苑食堂（563 米、4.1 分）、"
    "桑浦树屋（586 米、4.7 分、人均 20.00 元）。步行过去大约 8 分钟。",
    "用户：帮我存成文件吧",
    "助手：已存入「生成文库」。要改的话用 edit_office 工具传文件名就行。",
    "用户：我妹妹明年要高考了，我想帮她看看学校，先收集点资料",
    "助手：好的，需要我一起看吗？",
    "用户：等下，我寒假打算学一下 Rust，先把资料存起来，以后再说",
    "助手：明白。",
    "用户：对了，以后回答都先给结论再给理由，别绕",
])

known_long = ("用户姓名：陈宇桐，要求直接称呼全名；学校：广州民航职业技术学院，"
              "人工智能技术应用专业，大一新生；回答偏好：始终使用简体中文。")


def main():
    print("=" * 66)
    print("长期记忆提炼口径验证（临时数据目录：%s）" % TMP)
    print("=" * 66)
    cfg = config.load_config()
    model = cfg.get("default_model") or "qwen3-vl:8b"
    prompt = M._EXTRACT_PROMPT.format(long=known_long, short="（暂无）", convo=CONVO)

    print()
    print("合成对话里放了：")
    print("  A 餐厅评分（一次性查询结果）  ← 不该记")
    print("  B edit_office 用法（本应用操作说明）← 不该记")
    print("  C 妹妹明年高考 + 寒假想学 Rust  ← 该记")
    print("  D 回答要先结论后理由            ← 该记")
    print()
    print("正在跑提炼（模型 %s，可能要 1~4 分钟）…" % model)
    text = M._llm_extract(prompt, model, cfg)
    print()
    print("=" * 66)
    print("模型提炼出的内容：")
    print("=" * 66)
    print(text if text.strip() else "（空 —— 说明提炼被截断，是另一个问题）")
    print()

    low = text or ""
    print("=" * 66)
    print("逐项判定")
    print("=" * 66)
    # —— 不该出现的 ——
    for bad, why in [("4.7", "餐厅评分数字"), ("人均", "人均消费"),
                     ("第二、三饭堂", "一次性查到的店名"), ("西苑食堂", "一次性查到的店名"),
                     ("edit_office", "本应用的操作说明"), ("生成文库", "本应用的操作说明"),
                     ("步行", "一次性路线信息")]:
        check("没有记入「%s」（%s）" % (bad, why), bad not in low)
    # —— 应该出现的 ——
    check("记下了「妹妹」（身边的人）", "妹妹" in low or "高考" in low,
          "相关行：" + _line(low, ["妹妹", "高考"]))
    check("记下了「寒假 / Rust」（未来意图）",
          ("Rust" in low) or ("rust" in low.lower()) or ("寒假" in low),
          "相关行：" + _line(low, ["Rust", "rust", "寒假"]))
    check("记下了回答格式偏好", ("结论" in low) or ("理由" in low),
          "相关行：" + _line(low, ["结论", "理由"]))

    print()
    print("=" * 66)
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("   - " + f)
    else:
        print("全部通过 ✔  —— 该记的记了、不该记的没记")
    print("=" * 66)
    return 1 if FAIL else 0


def _line(text, keys):
    for ln in (text or "").splitlines():
        if any(k in ln for k in keys):
            return ln.strip()[:80]
    return "（没找到相关行）"


if __name__ == "__main__":
    sys.exit(main())
