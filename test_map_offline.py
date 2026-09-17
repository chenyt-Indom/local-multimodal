# -*- coding: utf-8 -*-
"""验证地图的「联网 / 离线」两种模式。

关键不是"能不能跑通"，而是证明：
  · 离线模式下 **一个网络请求都没发出去**（用 urlopen 钩子实测，不靠肉眼看代码）
  · 联网模式查过的地点/路线，切到离线后**能重放**
  · 离线时算不出真路线，必须给"直线距离"并标明不是实际道路

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
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, r"D:/local-multimodal-src")

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


def offline(v):
    mt.online = lambda: (not v)


print("=" * 64)
print("临时数据目录：%s" % TMP)
print("=" * 64)
print()

# ================= 1. 离线模式 =================
print("【1】离线模式：只能靠本地")
offline(True)
CALLS.clear()

r = mt.search_place("汕头大学")
check("搜到「汕头大学」", bool(r), r[0]["name"] + " (%.4f, %.4f)" % (r[0]["lat"], r[0]["lon"]) if r else "")
check("来源标成内置表", bool(r) and r[0].get("kind") == "builtin")
no_net("离线搜地名")

r2 = mt.search_place("广州塔")
check("搜到「广州塔」", bool(r2), r2[0]["name"] + " (%.4f, %.4f)" % (r2[0]["lat"], r2[0]["lon"]) if r2 else "")
no_net("离线搜地名 2")

r3 = mt.search_place("这个地名肯定不存在xyzzy")
check("查不到就返回空（不编造）", r3 == [])
no_net("离线查不存在的地名")

# 别名
r4 = mt.search_place("澄海")
check("别名「澄海」→ 汕头市澄海区", bool(r4) and "澄海" in r4[0]["name"], r4[0]["name"] if r4 else "")
no_net("离线用别名")

# 坐标直给
r5 = mt.geocode_one("23.354, 116.682")
check("直接给坐标也能定位", bool(r5) and r5.get("kind") == "coord", r5["name"] if r5 else "")
no_net("离线解析坐标")

# 没缓存过的路线 → 只能给直线距离
rt = mt.plan_route("汕头大学", "汕头站")
check("离线算不出真路线", rt.get("ok") is False)
check("但给出直线距离", bool(rt.get("approx")),
      "直线 %s，在起点%s方向" % (mt.fmt_distance(rt["approx"]["distance_m"]),
                                 rt["approx"]["bearing"]) if rt.get("approx") else "")
check("说明里讲清是离线", "离线" in (rt.get("error") or ""), rt.get("error") or "")
no_net("离线规划路线")

# 瓦片：没缓存就不给，而且不联网
d, cached = mt.get_tile(12, 3360, 1743)
check("离线取没缓存的瓦片 → 返回空", d is None)
no_net("离线取瓦片")

# 预下载在离线时必须被拒绝
st = mt.prefetch_route([[23.35, 116.68]], zoom=12)
check("离线时预下载被拒绝", bool(st.get("error")), st.get("error") or "")
no_net("离线预下载")

print()

# ================= 2. 联网模式 =================
print("【2】联网模式：上网查 + 自动存本地")
online(True)
CALLS.clear()

r6 = mt.search_place("汕头大学")
check("联网搜到「汕头大学」", bool(r6),
      r6[0]["name"] + " (%.4f, %.4f)" % (r6[0]["lat"], r6[0]["lon"]) if r6 else "")
# ⚠️ 「汕头大学」在内置表里，会**直接走表**（那 231 条人工核过，比 Photon 稳），
#    压根不发请求 —— 这是设计如此，不是 bug。
#    想验证"联网真的发出去了 + 写进缓存"，得拿一个**表外**、且 Photon 确实有数据的名字。
#    （踩过：用「汕头濠江滨海街道」这种，Photon 返回 0 条，结果又掉回内置表兜底，
#      看着像"缓存没写"，其实是那个词 Photon 压根不认识。）
r6b = mt.search_place("广州猎德大桥")
check("表外的地名能联网查到", bool(r6b), r6b[0]["name"] if r6b else "")
check("确实联网了（Photon）", any("photon" in u for u in CALLS), "请求数 %d" % len(CALLS))
CALLS.clear()

r7 = mt.search_place("广州天河城")
check("联网搜到「广州天河城」", bool(r7), r7[0]["name"] if r7 else "")
CALLS.clear()

rt2 = mt.plan_route("汕头大学", "汕头站", "driving")
check("联网规划出真路线", rt2.get("ok") is True,
      "%s，约 %s" % (mt.fmt_distance(rt2.get("distance_m", 0)),
                     mt.fmt_duration(rt2.get("duration_s", 0))) if rt2.get("ok") else rt2.get("error"))
check("路线有路径点", len(rt2.get("points") or []) > 1, "%d 个点" % len(rt2.get("points") or []))
check("确实联网了（OSRM）", any("osrm" in u or "project-osrm" in u for u in CALLS))
CALLS.clear()

# 预下载瓦片（走的是当前这套：真实数据目录里已有 106 张，临时目录里是 0）
st2 = mt.prefetch_route(rt2.get("points") or [[23.35, 116.68]], zoom=12, max_tiles=12)
check("联网预下载瓦片成功", not st2.get("error"),
      "请求 %d 张，新下 %d 张" % (st2.get("requested", 0), st2.get("new", 0)))
CALLS.clear()

print()

# ================= 3. 关掉联网后重放 =================
print("【3】切回离线：刚才查过的必须还能用（这是离线模式的意义）")
offline(True)
CALLS.clear()

# 用表外那条（联网时走了 Photon 并写了缓存）验证"断网还能重放"
r8 = mt.search_place("广州猎德大桥")
check("离线重放联网时查过的地名", bool(r8), r8[0]["name"] if r8 else "")
check("标记来源＝本地缓存", bool(r8) and r8[0].get("from_cache") is True)
check("带缓存时间", bool(r8) and r8[0].get("cache_ts"))
no_net("离线重放地名")

# 内置表里的地名离线当然也能查（走表，同样零请求）
r8b = mt.search_place("汕头大学")
check("内置表地名离线也能查", bool(r8b), r8b[0]["name"] if r8b else "")
no_net("离线查内置表地名")

rt3 = mt.plan_route("汕头大学", "汕头站", "driving")
check("离线重放出真路线", rt3.get("ok") is True,
      "%s，约 %s" % (mt.fmt_distance(rt3.get("distance_m", 0)),
                     mt.fmt_duration(rt3.get("duration_s", 0))) if rt3.get("ok") else rt3.get("error"))
check("标记为本地缓存", rt3.get("from_cache") is True)
check("路线点数是真路线（>1）", len(rt3.get("points") or []) > 1,
      "%d 个点" % len(rt3.get("points") or []))
no_net("离线重放路线")

# 换一条没查过的 → 必须诚实降级，不能拿缓存冒充
rt4 = mt.plan_route("汕头大学", "广州塔", "driving")
check("没缓存过的路线仍然算不出（不冒充）", rt4.get("ok") is False)
check("退回直线距离参考", bool(rt4.get("approx")))
no_net("离线规划新路线")

print()

# ================= 4. 缓存统计 =================
print("【4】本地缓存统计")
s = mt.cache_stats()
print("    瓦片 %d 张 / %.2f MB ｜ 地名 %d 个 ｜ 路线 %d 条 ｜ 内置表 %d 条 ｜ 当前 %s"
      % (s["tiles"], s["bytes"] / 1048576.0, s["places"], s["routes"],
         s["builtin"], mt.mode_text()))
# ⚠️ 只要求 >=1：内置表命中的地名**不会**写进缓存（压根没走网络），这是正常的
check("地名缓存已写入", s["places"] >= 1, "%d 个" % s["places"])
check("路线缓存已写入", s["routes"] >= 1, "%d 条" % s["routes"])
check("内置地名表已加载", s["builtin"] > 100, "%d 条" % s["builtin"])
check("离线模式被正确识别", s["online"] is False)

print()
print("=" * 64)
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("   - " + f)
else:
    print("全部通过 ✔")
print("=" * 64)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
