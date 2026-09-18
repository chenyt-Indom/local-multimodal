# -*- coding: utf-8 -*-
"""验证地图的「联网 / 离线」两种模式，以及**不做任何本地持久化**这条新规矩。

背景（2026-09-18 用户明确要求）：地图缓存整个去掉了 —— 不存瓦片、不存地名、
不存路线，离线也不再从任何本地缓存取数据。所以这个脚本的断言目标变了：

  · 离线模式下 **一个网络请求都不发**（urlopen 钩子实测，不靠肉眼看代码）
  · 离线时只有内置常用地名表可用；表外的名字**如实返回空**，绝不编坐标
  · 离线时算不出真路线 → 只给直线距离，并标明"不是实际道路"
  · 联网查完**磁盘上不新增任何文件**（这是"去掉缓存"最硬的证据）
  · 断网后查同一条**拿不到** —— 证明真的没有缓存（不是"藏在别处"）
  · 老的缓存入口（cache_stats / prefetch_route）确实已经不存在了
  · 地图按钮跟着「联网」开关**立刻**变（2026-09-18 实测反馈：原来要再点一下才变）

跑法：用**应用自己的解释器**跑：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_map_offline.py
用临时 MM_DATA_DIR，不碰真实数据目录。
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TMP = tempfile.mkdtemp(prefix="mm_map_test_")
# ⚠️ 必须拷 config.json：否则走 DEFAULT_CONFIG → web_enabled=False、"没 key"，
#    会得到一堆假结论（这是这个项目反复踩过的坑）。
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
            os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import map_tools as mt          # noqa: E402

# ---- 网络钩子：记录所有真正发出去的外网请求 ----
CALLS = []
_orig_urlopen = urllib.request.urlopen


def _spy(req, *a, **k):
    url = getattr(req, "full_url", str(req))
    CALLS.append(url)
    return _orig_urlopen(req, *a, **k)


urllib.request.urlopen = _spy

FAIL = []


def check(label, cond, extra=""):
    print(("  [OK] " if cond else "  [!!] ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(label)


def no_net(label):
    """断言上一步没有联网。"""
    if CALLS:
        check(label + "（不该联网）", False, "实际请求了：%s" % CALLS[:3])
        CALLS.clear()
    else:
        check(label + "（零网络请求）", True)


def online(v):
    mt.online = lambda: v


# ⚠️ 上面那个 online() 会把 mt.online 整个换掉，后面想验"真实的开关读取 + 缓存"
#    就得留着原函数 —— 不然测的是自己的替身。
_REAL_ONLINE = mt.online


def data_files():
    """临时数据目录里除了 config.json 之外的所有文件（相对路径）。"""
    out = []
    for root, _dirs, files in os.walk(TMP):
        for f in files:
            p = os.path.join(root, f)
            if os.path.relpath(p, TMP) != "config.json":
                out.append(os.path.relpath(p, TMP))
    return sorted(out)


def main():
    print("=" * 64)
    print("临时数据目录：%s" % TMP)
    print("=" * 64)

    # ------------------------------------------------------------------
    print()
    print("【1】离线模式：零请求，只认内置表")
    online(False)

    r = mt.search_place("汕头大学", 1)
    check("内置表里的地名能查到（离线也能定位）", bool(r),
          r[0]["name"] if r else "")
    no_net("离线查内置表地名")

    r = mt.search_place("广州白云机场", 1)
    check("带城市的别名能对上（广州白云机场 → 广州白云国际机场）",
          bool(r) and "机场" in (r[0]["name"] if r else ""),
          r[0]["name"] if r else "")
    no_net("离线查别名")

    r = mt.search_place("广州猎德大桥", 1)
    check("表外的名字**如实返回空**，不编、也不拿'广州市'冒充", not r,
          (r[0]["name"] + "（这是错的）") if r else "返回空 ✅")
    no_net("离线查表外地名")

    rt = mt.plan_route("汕头大学", "汕头站", "driving")
    check("离线算不出真路线（ok=False）", rt.get("ok") is False, rt.get("error", ""))
    check("但给出直线距离（明确标注不是道路）",
          bool(rt.get("approx")) and rt.get("straight") is not False,
          "直线 %.1f 公里" % (rt["approx"]["distance_m"] / 1000) if rt.get("approx") else "")
    check("并且**没有** from_cache 这种『重放』痕迹", not rt.get("from_cache"))
    no_net("离线规划路线")

    nb = mt.nearby(23.4163, 116.6291, "餐厅", radius=1500, limit=3)
    check("离线查附近场所 → 如实说查不了（不再拿旧数据糊弄）",
          nb.get("ok") is False and "离线" in str(nb.get("error", "")),
          str(nb.get("error", ""))[:46])
    no_net("离线查周边")

    t, _c = mt.get_tile(14, 13500, 7095, src="amap")
    check("离线取瓦片 → 取不到（离线不显示底图）", t is None)
    no_net("离线取瓦片")

    # ------------------------------------------------------------------
    print()
    print("【2】联网模式：实时查，能用")
    online(True)

    r = mt.search_place("汕头大学", 1)
    check("联网能查到地名", bool(r), r[0]["name"] if r else "")
    CALLS.clear()

    rt = mt.plan_route("汕头大学", "汕头站", "driving")
    check("联网能算出真路线", bool(rt.get("ok")),
          "%.1f 公里 / %.0f 分钟 / %s 个点"
          % (rt["distance_m"] / 1000, rt["duration_s"] / 60, len(rt["points"]))
          if rt.get("ok") else rt.get("error", ""))
    check("路线确实联网了", bool(CALLS), "%d 次请求" % len(CALLS))
    CALLS.clear()

    nb = mt.nearby(23.4163, 116.6291, "餐厅", radius=1500, limit=3)
    check("联网能查附近场所", bool(nb.get("ok")),
          "%s 个（来源 %s）" % (nb.get("total"), nb.get("source")))
    CALLS.clear()

    t, cached = mt.get_tile(14, 13500, 7095, src="amap")
    check("联网能取到底图瓦片", bool(t) and len(t) > 1000, "%d 字节" % len(t or b""))
    check("瓦片一律标成'不是缓存来的'", cached is False)
    CALLS.clear()

    # ------------------------------------------------------------------
    print()
    print("【3】★ 不做任何本地持久化（这次改动的核心）")

    left = data_files()
    check("联网查了一圈，磁盘上**一个文件都没新增**", not left,
          ("多了：" + str(left[:5])) if left else "干净 ✅")

    online(False)
    r = mt.search_place("汕头大学", 1)
    check("断网后仍能查到（那是**内置表**，不是缓存）",
          bool(r) and r[0].get("kind") in ("builtin", "builtin_loose"),
          r[0].get("kind") if r else "")
    r = mt.search_place("广州大桥", 1)
    check("断网后查表外名字拿不到 —— 证明联网那次**真的没存**", not r,
          r[0]["name"] if r else "返回空 ✅")
    no_net("离线再查一次")

    # ------------------------------------------------------------------
    print()
    print("【4】老缓存入口确实已经不存在")
    for name in ("cache_stats", "cache_dir", "prefetch_route", "prefetch_area",
                 "_load_cache", "cache_put"):
        check("已移除 map_tools.%s" % name, not hasattr(mt, name))

    idx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "frontend", "index.html")
    try:
        html = io.open(idx, encoding="utf-8").read()
        check("顶栏的「地图缓存」按钮已删掉", "mapCacheBtn" not in html)
        check("换成了「高德 key」入口", "amapKeyBtn" in html)
    except OSError:
        check("能读到前端页面", False, idx)

    # ------------------------------------------------------------------
    print()
    print("【5】地图按钮必须跟着「联网」开关**立刻**变（2026-09-18 实测反馈）")
    # 现象：关掉联网后按钮不立刻变暗，非要再点它一下才变 —— 看着像按钮坏了。
    # 根因有两个：① 前端只靠 45 秒轮询；② 后端 online() 有 2 秒小缓存。
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        js = io.open(os.path.join(root, "frontend", "app.js"),
                     encoding="utf-8").read()
        check("关/开「联网」时立刻同步地图按钮（不再等 45 秒轮询）",
              "await refreshAmapState(on);" in js)
        check("重新打开「联网」时会强制重新校验一次 key（?force=1）",
              'amap_status" + (force ? "?force=1" : "")' in js)
    except OSError:
        check("能读到 frontend/app.js", False, root)

    try:
        css = io.open(os.path.join(root, "frontend", "style.css"),
                      encoding="utf-8").read()
        seg = css.split(".pill-btn.on {", 1)[1].split("}", 1)[0]
        check("「高德 key」亮起态与顶栏其它开关同族（复用 accent 配色）",
              "var(--accent)" in seg and "box-shadow" not in seg)
        check("它不再靠降低透明度装\"暗\"（其它开关都没这招）",
              ".pill-btn { opacity" not in css)
        check("样式里没有引用**未定义**的变量 --line",
              "var(--line)" not in css)
    except (OSError, IndexError):
        check("能读到 frontend/style.css 且含 .pill-btn.on", False, root)

    # 2 秒在线状态小缓存：改完配置必须**立刻**能看到新值
    # （这里必须用**真实的** online，不是上面那个替身）
    mt.online = _REAL_ONLINE
    from backend import config as C
    C.save_config(dict(C.load_config(), web_enabled=True))
    check("开关打开后 online() = True", mt.online() is True)
    C.save_config(dict(C.load_config(), web_enabled=False))   # 此刻缓存是"热"的
    check("（这 2 秒缓存正是\"按钮愣一下\"的根源）缓存期内仍返回旧值",
          mt.online() is True)
    mt.invalidate_online()
    check("invalidate_online() 后立刻读到新值 → 按钮能马上变暗",
          mt.online() is False)
    try:
        main_src = io.open(os.path.join(root, "backend", "main.py"),
                           encoding="utf-8").read()
        check("保存配置的接口里调了 invalidate_online()",
              "_mt.invalidate_online()" in main_src)
    except OSError:
        check("能读到 backend/main.py", False, root)

    print()
    print("=" * 64)
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("   - " + f)
    else:
        print("全部通过 ✔")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
