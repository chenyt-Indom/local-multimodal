# -*- coding: utf-8 -*-
"""验证"天气只用中国气象局（经高德）"这条契约（2026-09-18 用户定的）。

背景：先试过 Open-Meteo（全球模式），实测它在国内**降水虚报很严重**
（10 城 × 3 天对比：37% 虚报下雨、0 漏报），中文城市名解析也弱；
后来换过一次中国气象局（高德转发），用户看完又让改回 Open-Meteo，
最后再改成**只用中国气象局、Open-Meteo 彻底不用**（连兜底都不要）。
所以这个脚本要守住的是"**再也不许悄悄用回 Open-Meteo**"。

必须成立的几件事：
  · 联网 + 配了 key → 数据源是**中国气象局**，实况/预报/观测时间齐全
  · **没配 key → 直接如实说"去点顶栏的高德 key"，绝不回退到别的源**
  · 境外地名 → 明说"只覆盖中国大陆"，不塞一个国内同名小地方
  · 关掉「联网」→ 拒答且**零网络请求**
  · 高德只有 **4 天**、且**没有体感/降水概率/降水量** → 格式化时绝不能打印 None，
    而且要在**最前面**写明"本次没有这些数据"（否则模型会自己编）
  · 地图卡片：出发地此刻（实况）+ 抵达**那天**的预报，都来自气象局
  · 源码里**一个 open-meteo 的 URL 都不许有**
  · ⚠️ 用户问「明天/后天」时**绝不能**因为模型把 days 填小了而丢掉那天
    （实测 bug：模型填 days=1 → 老实现从今天起截断 → 明天整条消失）

跑法（用应用自己的解释器）：
    "%LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe" test_weather.py
用临时 MM_DATA_DIR，不碰真实数据目录。
"""
import glob
import io
import os
import shutil
import sys
import tempfile
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="mm_weather_test_")
# ⚠️ 必须拷 config.json：里面才有真实的高德 key，否则测出来全是"没配 key"那条路
shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(TMP, "config.json"))
os.environ["MM_DATA_DIR"] = TMP
sys.path.insert(0, ROOT)

from backend import web_tools as W      # noqa: E402
from backend import config as C         # noqa: E402
from backend import map_tools as mt     # noqa: E402
from backend import amap as A           # noqa: E402
from backend import tools as T          # noqa: E402

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
    print("【1】联网 + 配了高德 key → 数据源必须是「中国气象局」")
    if not REAL_KEY:
        check("config.json 里有可用的高德 key（没有就没法测这一节）", False)
    set_cfg(web_enabled=True, amap_key=REAL_KEY)
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

    print()
    print("【2】境外地名：明说只覆盖国内，不许塞一个国内同名小地方")
    for q, bad in [("东京", "贵港"), ("伦敦", "佛山"), ("首尔", "重庆")]:
        r = W.weather(q, 1)
        blob = (r.get("city") or "") + (r.get("admin") or "")
        check("「%s」没返回 %s 的天气" % (q, bad), (not r.get("ok")) or bad not in blob,
              (r.get("city") or "") + " ｜ " + (r.get("error") or "")[:40])
        check("「%s」明说了只覆盖中国大陆" % q,
              (not r.get("ok")) and "中国大陆" in (r.get("error") or ""),
              (r.get("error") or "")[:44])

    print()
    print("【2.5】港澳台：数据源没覆盖，要**说清是覆盖问题**，且不许崩")
    # 实测（2026-09-18）：香港区级 810100 回 `"lives": [[]]`（空 list，直接取 [0] 会
    # AttributeError）、810000 的实况是空壳、`all`（预报）直接 status=0 UNKNOWN_ERROR。
    # 也就是高德**根本不提供**港澳台的天气 —— 要说清是数据覆盖问题，
    # 而不是笼统的"查不到"，更不能把内部异常抛给用户。
    for q in ["香港", "澳门", "台北"]:
        r = W.weather(q, 3)
        check("「%s」如实说明数据未覆盖" % q,
              (not r.get("ok")) and ("覆盖" in (r.get("error") or "")),
              (r.get("error") or "")[:52])
        check("「%s」没说成「境外」（港澳台是中国的一部分）" % q,
              "境外" not in (r.get("error") or ""))
    try:
        empty = A.live("810100")
        check("空结构（lives 为 [[]]）不会抛异常", empty == {}, str(empty))
    except Exception as e:
        check("空结构（lives 为 [[]]）不会抛异常", False, "抛了 %s" % e)
    try:
        A.forecast("810100")
        check("预报接口返回错误码时也不抛异常", True)
    except Exception as e:
        check("预报接口返回错误码时也不抛异常", False, "抛了 %s" % e)

    print()
    print("【3】没配 key → 如实让用户去配，**绝不回退到别的数据源**")
    set_cfg(amap_key="")
    CALLS.clear()
    w = W.weather("广州", 3)
    check("查不了（ok=False）", not w.get("ok"), w.get("city") or "")
    check("提示去点顶栏的「高德 key」", "高德 key" in (w.get("error") or ""),
          (w.get("error") or "")[:46])
    check("没有返回任何天气数据（没有偷偷回退）",
          not (w.get("daily") or w.get("current")))
    check("一个外网请求都没发", not CALLS, str(CALLS[:2]))
    set_cfg(amap_key=REAL_KEY)

    print()
    print("【4】关掉「联网」→ 拒答，且零请求")
    set_cfg(web_enabled=False)
    CALLS.clear()
    w = W.weather("广州", 1)
    check("离线直接拒答", (not w.get("ok")) and "离线" in (w.get("error") or ""),
          w.get("error"))
    check("离线时零网络请求", not CALLS, str(CALLS[:2]))
    set_cfg(web_enabled=True)

    print()
    print("【5】格式化：缺的字段不能打印 None，且要在**最前面**写明缺什么")
    w = W.weather("北京", 3)
    txt = W.format_weather(w)
    check("没有 None", "None" not in txt, txt.splitlines()[0][:46])
    check("写明了数据源", "气象局" in txt)
    check("开头点明了缺哪些字段", txt.lstrip().startswith("⚠️ 本次**没有**这些数据"),
          txt.splitlines()[0][:42])
    check("点名了体感温度/降水概率",
          "体感温度" in txt.splitlines()[0] and "降水概率" in txt.splitlines()[0])
    # 实测踩过：模型问"明天天气"时把**实况的湿度**搬进了"明天的预报"。
    # 逐日预报里根本没有湿度/风力这两项，搬过去就是编。
    check("实况那行写明了「只代表此刻、不是某天的预报」",
          "此刻实况" in txt and "任何一天的预报" in txt,
          [l.strip()[:44] for l in txt.splitlines() if "此刻实况" in l])

    print()
    print("【6】高德只有 4 天：要 6 天也只给 4 天，并且说清楚")
    w = W.weather("广州", 6)
    n = len(w.get("daily") or [])
    check("最多给 4 天", n == 4, "%d 天" % n)
    check("说明里讲了只有 4 天", "4 天" in (w.get("note") or ""), (w.get("note") or "")[-30:])

    print()
    print("【6.5】问「明天」绝不能被 days 参数截掉（2026-09-18 用户实测踩到的 bug）")
    # 现象：用户问「明天汕头天气」，回答却说"本次返回结果中仅显示今日信息"。
    # 根因：模型把 days 理解成"我要哪一天"，填了 1；而实现是**从今天起截断**，
    #       正好把用户要的明天砍掉。数据源本来就返回 4 天，截断纯属自伤。
    for d in (1, 0, 2):
        txt = T.dispatch("get_weather", {"city": "汕头", "days": d}, [], {})
        check("days=%s 时依然给出「明天」" % d, "（明天·" in txt,
              [l.strip() for l in txt.splitlines() if "2026-" in l][:3])
    txt = T.dispatch("get_weather", {"city": "汕头"}, [], {})
    check("表头写明今天是几号（模型据此把 09-19 认成「明天」）", "今天是" in txt,
          [l.strip() for l in txt.splitlines() if "今天是" in l][:1])
    check("每条预报都带「今天/明天/后天·周几」",
          "（今天·" in txt and "（明天·" in txt)
    check("不会再冒出「没提供明天的预报」这种话", "没提供明天" not in txt)

    from datetime import date as _d, timedelta as _td
    t0 = _d.today()
    check("标签按**真实日期**算（今天）", W._day_label(t0.strftime("%Y-%m-%d")).startswith("今天"),
          W._day_label(t0.strftime("%Y-%m-%d")))
    check("标签按**真实日期**算（明天）",
          W._day_label((t0 + _td(days=1)).strftime("%Y-%m-%d")).startswith("明天"))
    check("第 4 天不叫「第4天」，叫「3天后」",
          W._day_label((t0 + _td(days=3)).strftime("%Y-%m-%d")).startswith("3天后"),
          W._day_label((t0 + _td(days=3)).strftime("%Y-%m-%d")))
    check("日期解析不了也不炸（返回空标签）", W._day_label("") == "")
    # 地图卡的「抵达那天」是同一类错误的高发区：原来写的是
    # "等于今天就是今天、否则明天" —— 跨 2 天以上会被错说成"明天"，
    # 而 pick_day 找不到日期时退回今天那条，标签还会跟数据自相矛盾。
    check("地图卡「抵达那天」标签按真实日期算",
          mt._rel_day(t0.strftime("%Y-%m-%d")) == "今天"
          and mt._rel_day((t0 + _td(days=2)).strftime("%Y-%m-%d")) == "后天"
          and mt._rel_day((t0 + _td(days=5)).strftime("%Y-%m-%d")) == "5天后")
    check("地图卡标签解析不了也不炸", mt._rel_day("") == "")

    print()
    print("【7】地图卡片：实况 + 抵达**那天**的预报，都来自气象局")
    wa = mt.weather_at(23.1167, 113.2500)     # 广州
    check("有实况", bool(wa.get("now")), str(wa.get("now")))
    check("有逐日预报（给「抵达那天」用）", bool(wa.get("daily")),
          "共 %d 天" % len(wa.get("daily") or []))
    check("来源是气象局", "气象局" in (wa.get("src") or ""), wa.get("src"))
    check("不再有逐小时数据（那是 Open-Meteo 才有的）", not wa.get("hours"))
    d = mt.pick_day(wa.get("daily"), "2999-01-01")
    check("pick_day 查不到日期时会退回第一条（不返回空）", bool(d), str(list(d)[:3]))

    print()
    print("【8】契约：源码里不许再有 open-meteo 的接口地址")
    hits = []
    for f in glob.glob(os.path.join(ROOT, "backend", "*.py")) + \
             glob.glob(os.path.join(ROOT, "frontend", "*.js")):
        try:
            src = io.open(f, encoding="utf-8").read()
        except OSError:
            continue
        if "open-meteo.com" in src:
            hits.append(os.path.basename(f))
    check("没有任何 open-meteo 请求地址", not hits, str(hits))
    check("amap 里有请求节流（否则高德会随机返回空）",
          float(getattr(A, "_MIN_GAP", 0)) >= 0.2, getattr(A, "_MIN_GAP", None))

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
