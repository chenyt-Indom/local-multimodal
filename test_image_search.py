# -*- coding: utf-8 -*-
"""验证"搜图绝不返回不相关图片"这条底线（2026-09-21 用户报的问题）。

背景（用户截图）：问捕蝇草，界面弹出来的却是**光学透镜示意图**
（「正透镜 负透镜」「平凸柱面透镜 H-K9L」「几何光学笔记3 理想光学系统」），
模型还照着一本正经地做了"分析"。用户问"有没有办法解决"。

排查结论（实测，见下面 check 的注释）：
  根因不在查询词，在**图片源本身**。Bing 图片搜索在"无 JS 抓取"下不可靠：
    · 「捕蝇草」         → 35 条，全对
    · 「捕蝇草 植物」    → 1 条，内容是"地暖保温条"
    · 「维纳斯捕蝇草」   → 12 条，全是 Photoshop CS6 下载页
    · 「捕蝇草 结构」    → 12 条，全是"世界旅游胜地"
  加 Cookie、加 mkt/FORM 参数、换国际版都无效（都验过）。
  页面 title 是对的、结果区却是别的东西 —— 所以模型会把它们当真。
  360 图片的 JSON 接口同一批查询返回 57/117 条、标题全对。

所以这个脚本守两件事：
  A. **安全底线**：任何一个源整批与查询零重合时，必须丢掉、返回空，
     而不是把别的主题的图交出去（宁可模型说"没搜到"）。
  B. **能搜到**：常用查询要走 360 图片拿到真实的、标题对得上的原图。

跑法（用应用自己的解释器，不用占 8000）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_image_search.py
  联网部分默认跑；加 --offline 只跑不依赖网络的断言。
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_imgsearch_test_")
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import web_tools as W      # noqa: E402

OFFLINE = "--offline" in sys.argv

PASS = 0
FAIL = []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print(f"  [OK] {label}" + (f"  {extra}" if extra else ""))
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {extra}")


def has_cn(s, *words):
    return any(w in (s or "") for w in words)


# ==========================================================================
print("\n=== 1. 离线：垃圾批次必须被拦住（这是用户那个 bug 的入口）===")
# 直接注入一个"返回光学透镜"的假源 —— 复现用户截图里看到的东西。
# 关键：不是"少给几条"，而是**必须整批丢掉**。
FAKE_LENS = [
    {"title": "正透镜 负透镜", "url": "http://x/1.jpg", "thumb": "", "source": ""},
    {"title": "平凸柱面透镜 H-K9L 增透膜 - omtools", "url": "http://x/2.jpg",
     "thumb": "", "source": ""},
    {"title": "几何光学笔记3 理想光学系统 - 知乎", "url": "http://x/3.jpg",
     "thumb": "", "source": ""},
]
_real_sources = W._IMG_SOURCES
try:
    W._IMG_SOURCES = (("假源", lambda q, n: list(FAKE_LENS)),)
    out = W.image_search("捕蝇草", n=4)
    check("整批零重合 → 返回空，而不是把透镜图交出去", out == [], f"实得 {len(out)} 条")
    check("  （对照）这批图确实与'捕蝇草'零重合",
          not W._img_overlap("捕蝇草", FAKE_LENS))

    # 混批：真结果里混进一条无关的，无关的那条要被压到后面/丢掉
    MIXED = [dict(FAKE_LENS[0]),
             {"title": "捕蝇草图片大全 - 花卉网", "url": "http://x/good.jpg",
              "thumb": "", "source": ""}]
    W._IMG_SOURCES = (("假源", lambda q, n: list(MIXED)),)
    out = W.image_search("捕蝇草", n=4)
    check("混批时优先给相关的", bool(out) and "捕蝇草" in out[0]["title"],
          f"首条={out[0]['title'] if out else '-'}")
    check("混批时无关的那条被剔掉",
          all("透镜" not in r["title"] for r in out))
finally:
    W._IMG_SOURCES = _real_sources

check("空查询 → 空结果（不联网）", W.image_search("") == [])
check("空查询带空格 → 空结果", W.image_search("   ") == [])

print("\n=== 2. 离线：查询词收缩（兜底用的核心词）===")
check("「捕蝇草 植物」→ 收缩出「捕蝇草」", W._core_img_query("捕蝇草 植物") == "捕蝇草")
check("「捕蝇草 高清大图」清洗后不含'高清/大图'",
      "高清" not in W._clean_img_query("捕蝇草 高清大图")
      and "大图" not in W._clean_img_query("捕蝇草 高清大图"))
check("单个词不收缩（避免把「显微 镜」这类切成半截）",
      W._core_img_query("捕蝇草") == "")

print("\n=== 3. 离线：源列表顺序（360 必须排在 Bing 前面）===")
names = [nm for nm, _fn in W._IMG_SOURCES]
check("首选 360 图片", names and names[0] == "360图片", f"实得 {names}")
check("Bing 只作最后兜底", names and names[-1] == "Bing图片", f"实得 {names}")
import inspect                                                        # noqa: E402
_src = inspect.getsource(W.download_image)
check("download_image 的 referer 重试链里有 image.so.com（qhimg 只认它）",
      "image.so.com" in _src)

if not OFFLINE:
    print("\n=== 4. 联网：用户截图里的那几个查询，必须给对或给空 ===")
    CASES = ["捕蝇草", "捕蝇草 植物", "维纳斯捕蝇草", "捕蝇草 结构", "捕蝇草 养护"]
    BAD = ("透镜", "光学", "几何光学", "photoshop", "旅游胜地", "保温",
           "地暖", "CPU", "intel", "草莓")
    for q in CASES:
        try:
            rs = W.image_search(q, n=4)
        except Exception as e:
            check(f"{q!r} 不抛异常", False, f"{type(e).__name__}: {e}")
            continue
        titles = " ; ".join((r.get("title") or "") for r in rs)
        low = titles.lower()
        hit = has_cn(titles, "捕蝇草", "食虫植物", "维纳斯")
        dirty = [b for b in BAD if b.lower() in low]
        check(f"{q!r} 无垃圾内容", not dirty, f"命中垃圾词 {dirty} | {titles[:70]}")
        if rs:
            check(f"{q!r} 标题与查询对得上", hit, f"{titles[:70]}")
        else:
            print(f"       （该查询没取到图 —— 允许，模型会如实说没搜到）")

    print("\n=== 5. 联网：原图能真的下载下来 ===")
    rs = W.image_search("捕蝇草", n=3)
    check("拿到候选", bool(rs), f"{len(rs)} 条")
    ok_dl = 0
    for r in rs[:3]:
        try:
            raw = W.download_image(r["url"], referer=r.get("referer"))
        except Exception:
            raw = None
        if raw:
            ok_dl += 1
    check("至少 1 张能下载（防盗链没把结果全挡掉）", ok_dl >= 1,
          f"{ok_dl}/{min(3, len(rs))} 张成功")

    print("\n=== 6. 联网：乱七八糟的查询不许返回强相关幻觉 ===")
    junk = W.image_search("zzzqqqxx不存在的词组合9f3a", n=4)
    check("乱码查询 → 空或极少（不得凑一堆看着相关的）", len(junk) == 0,
          f"实得 {len(junk)} 条")
else:
    print("\n（--offline：跳过联网断言）")

print(f"\n{'='*56}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  ! " + f)
sys.exit(1 if FAIL else 0)
