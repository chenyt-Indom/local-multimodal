# -*- coding: utf-8 -*-
"""验证"查天气"换源之后的行为（2026-09-18）。

背景：用户反馈"天气获取不太准确"。实测同一时刻的广州 ——
  · 高德（中国气象局）：实况 晴 28℃；逐日预报 晴 / 多云 / 多云
  · Open-Meteo      ：实况 晴 29.7℃；逐日预报却写「小毛毛雨 / 毛毛雨 / 小毛毛雨」
全球模式在华南对降水虚报得厉害，实况也不是站点观测 —— 这就是"不准"的根源。
所以主入口改成：**优先中国气象局（经高德地图），拿不到才退回 Open-Meteo**。

这个脚本要守住的东西：
  · 联网 + 配了 key → 数据源必须是**中国气象局**，且实况/预报/观测时间齐全
  · **境外地名不能被"就近匹配"到国内小地方**：实测「东京」→ 广西平南、「伦敦」→
    佛山顺德伦敦镇、「首尔」→ 重庆潼南区，连 Photon 都能把「纽约」匹配成北京的
    「纽约豪园」。这些必须被挡掉，宁可如实说"查不到、请用英文名"
  · 没配 key → 回退 Open-Meteo，而且**兜底也要能查到「汕头」**（Open-Meteo 自己查不到）
  · 关掉「联网」→ 直接拒答且**零网络请求**（不能偷偷联网）
  · 两个源字段不一样（高德没体感/降水概率）→ 格式化时**绝不能打印 None**
  · 要的天数 > 高德能给的 → 用 Open-Meteo 补，并**逐日标明来源**
  · 高德有 QPS 限制 → 请求之间必须有节流（否则会"随机查不到"）

跑法（用应用自己的解释器）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_weather.py
用临时 MM_DATA_DIR，不碰真实数据目录。
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_weather_test_")
# ⚠️ 必须拷 config.json：里面才有真实的高德 key，否则测的是"没配 key"那条路
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import web_tools as W      # noqa: E402
from backend import config as C         # noqa: E402
from backend import map_tools as mt     # noqa: E402
from backend import amap as A           # noqa: E402

PASS = 0
FAIL = []


def check(label, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (label, ("　— " + str(extra)) if extra else ""))
    else:
        FAIL.append("%s%s" % (label, ("　— " + str(extra)) if extra else ""))
        print("  [!!] %s%s" % (label, ("　— " + str(extra)) if extra else ""))


# ---- 网络钩子：记录真正发出去的外网请求 ----
CALLS = []
_orig_urlopen = urllib.request.urlopen


def _spy(req, *a, **k):
    CALLS.append(getattr(req, "full_url", str(req)))
    return _orig_urlopen(req, *a, **k)


urllib.request.urlopen = _spy

REAL_KEY = C.load_config().get("amap_key") or ""


def set_cfg(**kw):
    C.save_config(dict(C.load_config(), **kw))
    mt.invalidate_online()          # 「联网」开关有 2 秒小缓存，改完要立刻作废


def main():
    print()
    print("【1】联网 + 配了高德 key → 应该走中国气象局")
    if not REAL_KEY:
        check("config.json 里有可用的高德 key（没有就没法测这一节）", False)
    set_cfg(web_enabled=True)
    w = W.weather("广州", 3)
    check("查询成功", bool(w.get("ok")), w.get("error") or w.get("city"))
    if w.get("ok"):
        check("数据源是中国气象局", "气象局" in (w.get("source") or ""), w.get("source"))
        cur = w.get("current") or {}
        check("有实况天气现象", bool(cur.get("desc")), cur.get("desc"))
        check("有实况气温", cur.get("temp") is not None, cur.get("temp"))
        check("带上观测时间", bool(w.get("report_time")), w.get("report_time"))
        check("逐日预报 ≥3 天", len(w.get("daily") or []) >= 3,
              "%d 天" % len(w.get("daily") or []))
        check("逐日都标了来源", all(d.get("src") for d in (w.get("daily") or [])),
              str([d.get("src") for d in (w.get("daily") or [])][:3]))

    print()
    print("【2】境外地名不能被「就近匹配」到国内小地方")
    # 实测会匹配错的组合：查询词 → 不该出现的国内城市
    for q, bad in [("东京", "贵港"), ("伦敦", "佛山"), ("首尔", "重庆")]:
        r = W.weather(q, 1)
        blob = (r.get("city") or "") + (r.get("admin") or "")
        check("「%s」没有返回 %s 的天气" % (q, bad),
              not r.get("ok") or bad not in blob,
              (r.get("city") or "") + " ｜ " + (r.get("source") or ""))
        if not r.get("ok"):
            check("「%s」如实说查不到，并提示用英文名" % q,
                  "英文名" in (r.get("error") or ""), (r.get("error") or "")[:40])

    # 「纽约」高德的 POI 搜索会命中北京的住宅小区「纽约豪园」
    r = W.weather("纽约", 1)
    bl = r.get("lat"), r.get("lon")
    is_bj = (r.get("ok") and bl[0] and 39.4 <= bl[0] <= 41.1 and 115.4 <= bl[1] <= 117.6)
    check("「纽约」没被解析成北京的同名小区", not is_bj, "坐标 %s" % (bl,))

    print()
    print("【3】兜底源（Open-Meteo）也要能查到「汕头」")
    # Open-Meteo 自己的地理编码查不到「汕头」（要写"汕头市"），得靠我们自己的地名表兜
    set_cfg(amap_key="")
    w = W.weather("汕头", 2)
    check("没 key 时回退 Open-Meteo", "Open-Meteo" in (w.get("source") or ""), w.get("source"))
    check("兜底路径也能查到「汕头」", bool(w.get("ok")), w.get("error") or w.get("city"))

    print()
    print("【4】关掉「联网」→ 拒答，且一个请求都不发")
    set_cfg(web_enabled=False)
    CALLS.clear()
    w = W.weather("广州", 1)
    check("离线直接拒答", (not w.get("ok")) and "离线" in (w.get("error") or ""),
          w.get("error"))
    check("离线时零网络请求", not CALLS, str(CALLS[:2]))

    print()
    print("【5】格式化：两个源字段不一样，也不能打印 None")
    set_cfg(web_enabled=True, amap_key=REAL_KEY)
    w = W.weather("北京", 3)
    txt = W.format_weather(w)
    check("格式化文本里没有 None", "None" not in txt, txt.splitlines()[0][:50])
    check("文本里写明了数据源", "气象局" in txt)
    # 高德不给体感/降水概率 —— 必须在**开头**点明"没有这些数据"，
    # 否则实测模型会自己编出「体感温度约 29.5°C」「降水概率 0%」
    if "气象局" in (w.get("source") or ""):
        check("高德那条路：开头点明了缺哪些字段",
              txt.lstrip().startswith("⚠️ 本次**没有**这些数据"),
              txt.splitlines()[0][:44])
        check("并且点名了体感温度/降水概率",
              "体感温度" in txt.splitlines()[0] and "降水概率" in txt.splitlines()[0])

    print()
    print("【5b】Open-Meteo 那条路（字段齐全）不该出现这句警告")
    set_cfg(amap_key="")
    w2 = W.weather("汕头", 3)
    txt2 = W.format_weather(w2)
    if w2.get("ok"):
        check("Open-Meteo 结果里没有'缺字段'警告", "本次**没有**这些数据" not in txt2,
              txt2.splitlines()[0][:40])
    set_cfg(amap_key=REAL_KEY)

    print()
    print("【6】要 6 天 → 高德给多少算多少，其余用 Open-Meteo 补并逐日标来源")
    w = W.weather("广州", 6)
    days = w.get("daily") or []
    check("能给出 6 天", len(days) == 6, "%d 天" % len(days))
    srcs = [d.get("src") or "" for d in days]
    check("补的那几天标了 Open-Meteo", any("Open-Meteo" in s for s in srcs), str(srcs))
    check("高德那几天标了气象局", any("气象局" in s for s in srcs), str(srcs))

    print()
    print("【7】地图卡片：实况走气象局，逐小时仍走 Open-Meteo（并标出来源）")
    wa = mt.weather_at(23.1167, 113.2500)     # 广州
    check("有实况", bool(wa.get("now")), str(wa.get("now")))
    check("实况来自气象局", "气象局" in (wa.get("now_src") or ""), wa.get("now_src"))
    check("逐小时标了 Open-Meteo", (wa.get("hours_src") or "") == "Open-Meteo",
          wa.get("hours_src"))

    print()
    print("【8】契约：高德请求必须节流（不然会随机查不到）")
    check("amap 里有最小请求间隔", float(getattr(A, "_MIN_GAP", 0)) >= 0.2,
          getattr(A, "_MIN_GAP", None))
    check("amap 里有排队闸门 _wait_turn", callable(getattr(A, "_wait_turn", None)))
    check("天气返回结构里有 source 字段", "source" in (w or {}))

    # 收尾：把临时配置恢复成"有 key、联网"的样子（下次跑脚本不受影响）
    set_cfg(web_enabled=True, amap_key=REAL_KEY)
    A.invalidate_health()

    print()
    print("=" * 64)
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("   - " + f)
    else:
        print("全部通过 ✔  （%d 项）" % PASS)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
