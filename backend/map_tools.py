# -*- coding: utf-8 -*-
"""地图能力：找地点（地理编码）、规划路线、把瓦片**缓存到本地**。

为什么这么选（都实测过）：
· 地理编码 → **Photon**（`photon.komoot.io`）：免费、不用 key、WGS-84、中文能搜。
  ⚠️ **千万别加 `&lang=zh`** —— 加上之后中文查询全部返回空（实测踩到，查了半天）。
· 路线规划 → **OSRM**（`router.project-osrm.org`）：免费、不用 key、全球覆盖，
  实测"广州塔 → 白云机场"= 39.8 km / 31 分钟 / 819 个路径点。
· 瓦片 → OSM 系镜像（`tile.openstreetmap.org` 在国内**连不上**，换 `.fr` / `.de` 镜像）。
  全部走本模块的**本地缓存**：第一次看从网上取，之后离线也能看，
  还能按路线**预下载**一片区域（就是用户说的"在本地下载一个可以更新的地图"）。
· 三者都是 **WGS-84**，不会出现国内地图那种"路线和道路对不上"的偏移问题。

坐标一律用 (lat, lon) 对外，内部按各自 API 的要求转换。

------------------------------------------------------------
两种运行模式：跟着前端那个「联网」开关走
------------------------------------------------------------
· **联网模式**（web_enabled=True）：先看本地缓存，没有再上网取，取回来顺手存下。
· **离线模式**（web_enabled=False）：**一个网络请求都不发**。只用
  ① 本地缓存（以前查过的地名 / 算过的路线 / 下过的瓦片）
  ② 内置常用地名表（backend/builtin_places.py，几百条常用城市/机场/车站/高校/景点）

  所以离线时：查过的地名搜得到、算过的路线能重放、看过的瓦片出得了图；
  没查过的地名靠内置表兜底；**没算过的路线只能给直线距离**
  （会明确标注"不是实际道路"，绝不假装是真路线）。

⚠️ 别在离线分支里"顺手"发一个请求 —— 用户关掉开关就是不想联网。
   判断一律走 online()，不要在别处自己读 config，免得两处逻辑走岔。
"""
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request

_UA = "local-multimodal-assistant/1.0 (personal use)"
_TIMEOUT = 15

PHOTON = "https://photon.komoot.io/api/"
OSRM = "https://router.project-osrm.org/route/v1/%s/%s"
# 按顺序试，前面不通就用后面（实测 tile.openstreetmap.org 在这台机器上超时）
TILE_MIRRORS = [
    "https://a.tile.openstreetmap.fr/hot/%d/%d/%d.png",
    "https://tile.openstreetmap.de/%d/%d/%d.png",
    "https://a.tile.openstreetmap.org/%d/%d/%d.png",
]

# 高德底图（联网+配了 key 时的首选）。实测 0.1 秒一张，比 OSM 镜像快一个量级。
# style=7 是标准中文路网图（有中文路名、POI 标注）。
# ⚠️⚠️ 占位符顺序必须是 **z / x / y**（跟上面 TILE_MIRRORS 一致）——
#    取图那里是 `tpl % (z, x, y)` 统一喂进去的。写成 `&x=%d&y=%d&z=%d` 会静默错位：
#    实测表现是**地图整片空白**（高德对越界瓦片返回 179 字节的纯色图，不报错），
#    查了半天才发现是自己把参数喂反了。查询串的顺序本来无所谓，别手贱改。
# ⚠️⚠️ 高德瓦片是 **GCJ-02** 的，和 OSM 的 WGS-84 差 50~500 米 ——
#    所以两套底图的瓦片**绝不能共用缓存目录**，前端打点也必须跟着换坐标系
#    （见 tile_source / 前端 wgs2gcj）。混用会看到"点位整体偏出去一个街区"。
AMAP_TILE_MIRRORS = [
    "https://wprd01.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
    "https://wprd02.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
    "https://wprd03.is.autonavi.com/appmaptile?z=%d&x=%d&y=%d&lang=zh_cn&size=1&style=7",
]

MAX_ZOOM = 19


# ---------------------------------------------------------------- 路径
def cache_dir() -> str:
    """瓦片缓存目录：<数据根>/data/map_cache/"""
    try:
        from . import config
        # ⚠️ 要写 `config.data("data", ...)` —— 数据都在 <数据根>/data/ 下面，
        #    只写一层的话会跑到项目根目录去（实测踩到）。
        d = config.data("data", "map_cache")
    except Exception:
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "map_cache")
    os.makedirs(d, exist_ok=True)
    return d


def cache_stats() -> dict:
    """本地地图数据攒了多少 —— 这个直接决定"离线模式有多能打"。"""
    d = cache_dir()
    n = size = 0
    n_osm = n_amap = 0
    amap_dir = os.path.join(d, "amap")
    for root, _dirs, files in os.walk(d):
        is_amap = root == amap_dir or root.startswith(amap_dir + os.sep)
        for f in files:
            if f.endswith(".png"):
                n += 1
                if is_amap:
                    n_amap += 1
                else:
                    n_osm += 1
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    geo = _load_cache(_GEO_FILE)
    rts = _load_cache(_ROUTE_FILE)
    newest = 0.0
    for c in (geo, rts):
        for v in c.values():
            try:
                newest = max(newest, float((v or {}).get("ts") or 0))
            except (TypeError, ValueError):
                pass
    return {"tiles": n, "bytes": size, "dir": d,
            "tiles_osm": n_osm, "tiles_amap": n_amap,
            "places": len(geo), "routes": len(rts),
            "builtin": builtin_count(),
            "newest_ts": newest,
            "newest_ago": age_text(newest),      # 本地地图数据有多新
            "online": online(),
            "tile_source": tile_source(),
            "tile_source_name": tile_source_text(),
            "ttl_days": {"geo": _TTL_GEO // 86400,
                         "route": _TTL_ROUTE // 86400,
                         "tile": _TTL_TILE // 86400}}


# ---------------------------------------------------------------- 运行模式
# 短缓存：离线时前端一次要拉几十张瓦片，每张都去读一遍配置文件太浪费。
# 2 秒的窗口对用户完全无感（拨开关最多 2 秒后就生效），却能省掉几十次文件读取。
_ONLINE_CACHE = {"t": -1e9, "v": False}
_ONLINE_TTL = 2.0


def online() -> bool:
    """当前是否允许联网 —— 跟随前端那个「联网」开关（web_enabled）。

    地图三件事（找地点 / 规划路线 / 取瓦片）全都看这个。
    """
    now = time.time()
    if now - _ONLINE_CACHE["t"] < _ONLINE_TTL:
        return _ONLINE_CACHE["v"]
    try:
        from . import config
        v = bool(config.load_config().get("web_enabled", False))
    except Exception:
        # 读不到配置时按"不联网"处理：宁可少做事，也不能偷偷联网。
        v = False
    _ONLINE_CACHE["t"] = now
    _ONLINE_CACHE["v"] = v
    return v


def mode_text() -> str:
    return "联网模式" if online() else "离线模式"


def tile_source(allow_net=None) -> str:
    """底图用哪一个 —— "amap"（高德，GCJ-02）还是 "osm"（WGS-84）。

    **联网 + 配了 key → 高德**（中文路网图，实测比 OSM 镜像快一个量级）；
    其余情况（离线、没配 key）→ OSM。

    离线为什么还用 OSM：本地离线缓存的那几百张瓦片就是 OSM 的，
    换成高德等于把用户攒了半天的离线地图全作废。所以"联网用高德、
    离线看已缓存的 OSM"，各用各的坐标系，互不干扰。
    """
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return "osm"
    try:
        from . import amap
        return "amap" if amap.has_key() else "osm"
    except Exception:
        return "osm"


def tile_source_text(src: str = "") -> str:
    return "高德地图" if (src or tile_source()) == "amap" else "OpenStreetMap"


def _source_tag(net: bool) -> str:
    """当前实际会用哪个数据源 —— **只用来区分缓存键**。

    为什么要它：高德和 OSM 的结果质量差很多（有没有评分、POI 密度差十倍），
    共用一个缓存键会导致「刚配好高德 key，看到的却还是旧的 OSM 数据」（实测踩过）。
    """
    if not net:
        return "off"
    try:
        from . import amap
        return "amap" if amap.has_key() else "osm"
    except Exception:
        return "osm"


# ---------------------------------------------------------------- 结果缓存
# 为什么要它：离线模式能干什么，全看这里攒了多少东西。
# 联网查过的地名、算过的路线都留在本地，之后断网还能重放。
_GEO_FILE = "geocode.json"
_ROUTE_FILE = "routes.json"

# 缓存时效（秒）。到期后**联网时会自动重新拉一遍** —— 这就是"自动检查并更新本地地图数据"。
# 离线时不删旧的，只是继续用、并标明"数据来自 X 天前"：有旧数据也比没有强。
_TTL_GEO = 30 * 86400     # 地名：行政区划、店名变化慢
_TTL_ROUTE = 7 * 86400    # 路线：路网会变（修路、新开通）
_TTL_TILE = 60 * 86400    # 瓦片：底图更新慢


def _fresh(ts, ttl: float) -> bool:
    try:
        return (time.time() - float(ts or 0)) < ttl
    except (TypeError, ValueError):
        return False


def age_text(ts) -> str:
    """把时间戳说成「3 天前」这种人话，用来告诉用户这份数据有多旧。"""
    try:
        d = time.time() - float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if d < 0:
        return ""
    if d < 3600:
        return "%d 分钟前" % max(1, int(d // 60))
    if d < 86400:
        return "%d 小时前" % int(d // 3600)
    return "%d 天前" % int(d // 86400)


def _now() -> float:
    return time.time()


def _ckey(s) -> str:
    """缓存键归一化：去空格、转小写。"""
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def _cache_file(name: str) -> str:
    return os.path.join(cache_dir(), name)


def _load_cache(name: str) -> dict:
    try:
        with open(_cache_file(name), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(name: str, data: dict) -> None:
    """原子写：先写 .tmp 再 replace，避免写一半断电把缓存弄坏。"""
    p = _cache_file(name)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def cache_put(name: str, key: str, value) -> None:
    d = _load_cache(name)
    d[_ckey(key)] = value
    # 别让它无限膨胀：超过 3000 条时砍掉最旧的一半
    if len(d) > 3000:
        items = sorted(d.items(), key=lambda kv: (kv[1] or {}).get("ts") or 0)
        d = dict(items[len(items) // 2:])
    _save_cache(name, d)


# ---------------------------------------------------------------- 内置地名表
_BUILTIN = None


def _builtin():
    """(地名表, 别名表)；模块缺失时返回空表，不影响别的功能。"""
    global _BUILTIN
    if _BUILTIN is None:
        try:
            from .builtin_places import BUILTIN_PLACES, ALIASES
            _BUILTIN = (BUILTIN_PLACES or {}, ALIASES or {})
        except Exception:
            _BUILTIN = ({}, {})
    return _BUILTIN


def builtin_count() -> int:
    return len(_builtin()[0])


def _builtin_search(query: str, limit: int = 5) -> list:
    """在内置表里找。匹配分四档：完全相同 > 表中名以它开头 > 它出现在表中名里 > 表中名出现在它里。

    排序按 (档位, 名字长度) —— 同一档里短名字更通用，排前面。

    别名只管两件事：① 精确别名（"汕大"→"汕头大学"）；
    ② **常规匹配一个都没命中时**，再试"查询词里含某个别名"
       （"广州白云机场"里含"白云机场" → 广州白云国际机场）。
    ⚠️ ② 必须放在最后：否则「中山大学」会被别名「中山」抢到「中山市」去。
    """
    places, aliases = _builtin()
    if not places:
        return []
    key = _ckey(query)
    if not key:
        return []
    out, seen = [], set()

    def _hit(info, kind="builtin"):
        return {"name": info["name"], "lat": info["lat"], "lon": info["lon"],
                "addr": info.get("addr") or "", "kind": kind}

    # ① 精确别名
    al = aliases.get(str(query).strip()) or aliases.get(key)
    if al and al in places:
        out.append(_hit(places[al]))
        seen.add(al)

    cands = []
    for name, info in places.items():
        if name in seen:
            continue
        n = _ckey(name)
        if n == key:
            s = 0
        elif n.startswith(key):
            s = 1
        elif key in n:
            s = 2
        elif n in key:
            s = 3
        else:
            continue
        cands.append((s, len(name), name, info))
    cands.sort(key=lambda x: (x[0], x[1]))
    for _s, _l, name, info in cands:
        out.append(_hit(info))
        if len(out) >= limit:
            break

    # ② 一个都没命中，才试"查询词里含别名"。长的别名优先，
    #    免得短别名（如"中山"）把更具体的词带偏。
    if not out:
        for a in sorted(aliases.keys(), key=len, reverse=True):
            if len(a) >= 2 and a in str(query) and aliases[a] in places:
                out.append(_hit(places[aliases[a]]))
                break

    return out[:limit]


# ---------------------------------------------------------------- 天气（按坐标）
# 路线规划带上天气：知道出发/到达时段会不会下雨，比只给一个公里数有用得多。
# 用 open-meteo（免费、不要 key，和 get_weather 同一个源），这里多要一份**逐小时**数据。
_WMO = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
        51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨",
        65: "大雨", 66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
        77: "雪粒", 80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
        95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹"}


def wmo_text(code) -> str:
    try:
        return _WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def weather_at(lat: float, lon: float, hours: int = 12) -> dict:
    """按坐标查当前 + 未来几小时的天气。失败返回 {}（不抛异常，路线照常给）。"""
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
           "&current=temperature_2m,precipitation,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability,weather_code"
           "&timezone=Asia%%2FShanghai&forecast_days=2"
           % (lat, lon))
    try:
        d = _get_json(url, timeout=15)
    except Exception:
        return {}
    cur = d.get("current") or {}
    out = {"now": {"temp": cur.get("temperature_2m"),
                   "desc": wmo_text(cur.get("weather_code")),
                   "rain_mm": cur.get("precipitation"),
                   "wind": cur.get("wind_speed_10m")}}
    hr = d.get("hourly") or {}
    times = hr.get("time") or []
    n = len(times)
    rows = []
    for i, t in enumerate(times[:hours]):
        rows.append({"t": str(t)[11:16],
                     "temp": (hr.get("temperature_2m") or [None] * n)[i],
                     "rain": (hr.get("precipitation_probability") or [None] * n)[i],
                     "desc": wmo_text((hr.get("weather_code") or [None] * n)[i])})
    out["hours"] = rows
    return out


def pick_hourly(w: dict, offset_minutes: float) -> dict:
    """从逐小时里挑出"从现在起 offset 分钟之后"那一格的天气。"""
    rows = (w or {}).get("hours") or []
    if not rows:
        return {}
    idx = int(max(0.0, float(offset_minutes or 0)) // 60)
    return rows[min(idx, len(rows) - 1)]


# ---------------------------------------------------------------- 直线距离（离线兜底）
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点直线距离（米）。离线时算不了路网，但至少能告诉用户"大概多远、什么方向"。"""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_DIRS = ["正北", "东北", "正东", "东南", "正南", "西南", "正西", "西北"]


def bearing_text(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """B 在 A 的哪个方向。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return _DIRS[int((deg + 22.5) % 360 // 45)]


# ---------------------------------------------------------------- HTTP
def _get(url: str, timeout: int = _TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif r.headers.get("Content-Encoding") == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return raw


def _get_json(url: str, timeout: int = _TIMEOUT):
    return json.loads(_get(url, timeout).decode("utf-8", "replace"))


# ---------------------------------------------------------------- 找地点
def _addr(p: dict) -> str:
    parts = [p.get("street"), p.get("housenumber"), p.get("district"),
             p.get("city"), p.get("county"), p.get("state"), p.get("country")]
    return "".join(str(x) for x in parts if x)


def _builtin_exact(query: str):
    """内置表里**精确**命中（同名或别名）才返回，否则 None。

    联网时优先用它：这 231 条是人工核对过坐标的，比 Photon 稳 ——
    实测查「广州白云机场」，Photon 只给「广州白云机场综合保税区东区」这种
    保税区（坐标根本不在航站楼），而表里能直接命中机场。

    ⚠️ 别名匹配放在**精确匹配之后**，而且是"长的别名优先"：
    否则「中山大学」会被别名「中山」抢到「中山市」去。
    """
    places, aliases = _builtin()
    if not places:
        return None
    k = _ckey(query)

    def _pack(name):
        info = places[name]
        return {"name": info["name"], "lat": info["lat"], "lon": info["lon"],
                "addr": info.get("addr") or "", "kind": "builtin"}

    # ① 同名
    for name in places:
        if _ckey(name) == k:
            return _pack(name)
    # ② 正是别名
    al = aliases.get(str(query).strip()) or aliases.get(k)
    if al and al in places:
        return _pack(al)
    # ③ 查询词里**含**某个别名（"广州白云机场"含"白云机场"）
    for a in sorted(aliases.keys(), key=len, reverse=True):
        if len(a) >= 3 and a in str(query) and aliases[a] in places:
            return _pack(aliases[a])
    return None


def search_place(query: str, limit: int = 5, allow_net=None, city: str = "") -> list:
    """按名字找地点，返回 [{name, lat, lon, addr, kind}]。

    顺序：① 本地缓存 → ② **内置表精确命中** → ③ 联网查（高德优先）→ ④ 内置表模糊兜底。
    离线时（allow_net=False，或「联网」开关关着）③ 跳过，其余照常。
    返回项里带 from_cache / cache_ts 便于上层告诉用户"这是本地数据"。

    city：限定城市（用户说"广州市内""汕头有什么"时由调用方传进来）。
    """
    q = str(query or "").strip()
    if not q:
        return []
    limit = max(1, min(10, int(limit or 5)))
    net = online() if allow_net is None else bool(allow_net)

    # ① 本地缓存：没过期就直接用；**过期了在联网时会重查一遍**（这就是"自动更新"）
    ck = _source_tag(net) + "|" + (str(city or "").strip() + "|" if city else "") + q
    hit = _load_cache(_GEO_FILE).get(_ckey(ck))
    if hit and hit.get("results") and _fresh(hit.get("ts"), _TTL_GEO):
        return [dict(r, from_cache=True, cache_ts=hit.get("ts"))
                for r in hit["results"][:limit]]

    # ② 内置表精确命中：人工核过的名字与坐标，比 Photon 稳，还省一次请求
    ex = _builtin_exact(q)
    if ex:
        return [ex]

    # ②b 联网且配了高德 key：**先关键词搜 POI，搜不到再当地址解析**
    #     ⚠️ 顺序反了会出大错（实测）：查「天河城」时 /geocode 把它当地址，
    #     命中"江西省南昌市进贤县天河城"—— 那边真有个叫天河城的村子。
    #     凡是想找"某个地方"而不是"某个门牌号"，都该先走 /place/text。
    if net:
        try:
            from . import amap
            if amap.has_key():
                hits = []
                c0 = str(city or "").strip()
                if c0:
                    kw = q            # 调用方已经说了城市，整个查询词就是关键词
                else:
                    c0, kw = _split_city(q)   # 从"广州市天河城"里抠出 广州 + 天河城
                if kw and (c0 or len(kw) >= 2):
                    r = amap.place_text(kw, city=c0)
                    hits = [dict(x, kind=x.get("kind") or "poi")
                            for x in (r.get("items") or [])]
                if not hits:
                    hits = amap.geocode(q)        # 兜底：当地址解析
                    # ⚠️ 这一步是"按地址解析"，对**地点名**很不可靠：实测查「天河城」
                    #    会命中"江西省南昌市进贤县天河城"（那边真有个同名村子）。
                    #    所以查询词不像地址（没有"路/街/号/市/区/县/镇/村"）时打个标记，
                    #    让上层把"可能是同名地点、建议补城市名"说出来 ——
                    #    宁可说"不确定"，也不能默默给个外省结果。
                    if hits and not re.search(r"[路街巷号市区县镇村]", q):
                        hits = [dict(h, loose=True) for h in hits]
                if hits:
                    cache_put(_GEO_FILE, _ckey(ck), {"ts": _now(), "results": hits})
                    return hits[:limit]
        except Exception:
            pass

    # ③ 联网查 ⚠️ 不要带 &lang=zh —— 带了中文查询会全部返回空
    if net:
        url = "%s?q=%s&limit=%d" % (PHOTON, urllib.parse.quote(q), limit)
        try:
            d = _get_json(url)
            out = []
            for f in (d.get("features") or []):
                try:
                    lon, lat = f["geometry"]["coordinates"][:2]
                    p = f.get("properties") or {}
                    nm = p.get("name") or q
                    # Photon 有时给一个**比查询词更泛**的名字：查「汕头站」回「汕头」
                    # （坐标是对的，但名字对不上，用户会以为自己查错了）。
                    # 这种时候用用户自己的说法更准。
                    if nm and nm in q and len(nm) < len(q):
                        nm = q
                    out.append({"name": nm, "lat": float(lat),
                                "lon": float(lon), "addr": _addr(p),
                                "kind": p.get("osm_value") or p.get("osm_key") or ""})
                except Exception:
                    continue
            if out:
                cache_put(_GEO_FILE, _ckey(ck), {"ts": _now(), "results": out})
                return out[:limit]
        except Exception:
            pass      # 网断了 / Photon 挂了 → 继续往下走兜底

    # ④ 联网没成功（或本来就离线）：过期的缓存也照用，只是标明它有多旧
    if hit and hit.get("results"):
        return [dict(r, from_cache=True, cache_ts=hit.get("ts"), stale=True)
                for r in hit["results"][:limit]]

    # ⑤ 兜底：内置常用地名表（离线时主力）
    return _builtin_search(q, limit)


def _parse_latlon(s: str):
    """支持直接给 "23.35,116.68" 这种坐标 —— 离线时没有地名表也能定位。"""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*$", str(s or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        return {"name": "%.5f, %.5f" % (lat, lon), "lat": lat, "lon": lon,
                "addr": "", "kind": "coord"}
    return None


def geocode_one(query: str, allow_net=None, city: str = ""):
    """只要最匹配的一个，返回 dict 或 None。

    city 用来限定城市（用户说"广州市内有什么商场"时，把广州带下来，
    否则"天河城"会被解析成江西进贤县那个同名村子 —— 实测踩过）。
    """
    c = _parse_latlon(query)
    if c:
        return c
    r = search_place(query, limit=1, allow_net=allow_net, city=city)
    if not r:
        return None
    return r[0]


# ---------------------------------------------------------------- 路线
# 步行/骑行：OSRM 的免费服务**只加载了驾车路网**，请求 bike/foot 会静默回退到 car profile。
# 实测同一路线 driving/bike/foot 返回**完全一样**的 11.68km/12分钟 ——
# 12 分钟骑 11.7 公里等于 58km/h，显然给的就是驾车数据。
# 所以这里**一律按驾车取路**，步行/骑行只把**时间**按速度换算，
# 并明确标成估算（estimated=True），绝不假装那是真的步行路线。
_WALK_KMH = 4.8
_BIKE_KMH = 15.0


def plan_transit(origin: str, dest: str, allow_net=None) -> dict:
    """**公交换乘方案** —— 只有高德能给，OpenStreetMap 完全没有这类数据。

    高德的公交接口要求传起点城市（city 参数），所以先从地理编码结果里拿。
    """
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return {"ok": False, "offline": True,
                "error": "公交查询必须联网 —— 离线时本地没有公交数据。"}
    try:
        from . import amap
        if not amap.has_key():
            return {"ok": False, "error": "公交查询需要高德 key（在设置里填上即可）。"}
        a = geocode_one(origin, allow_net=net)
        b = geocode_one(dest, allow_net=net)
        if not a or not b:
            return {"ok": False,
                    "error": "找不到%s" % ("起点" if not a else "终点")}
        r = amap.transit(a["lat"], a["lon"], b["lat"], b["lon"],
                         city=a.get("city") or "")
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "没找到公交方案"}
        r["from"] = {"name": a["name"], "lat": a["lat"], "lon": a["lon"]}
        r["to"] = {"name": b["name"], "lat": b["lat"], "lon": b["lon"]}
        return r
    except Exception as e:
        return {"ok": False, "error": "公交查询失败：%s" % e}


def _route_amap(a: dict, b: dict, m: str):
    """走高德路径规划。没配 key / 失败 → 返回 None，调用方自动回退 OSRM。

    ⚠️ 关键差别：高德的步行/骑行是**真实路径**（会走人行道、小路），
       不像免费 OSRM 只能给驾车路网、再拿速度换算时间。
       所以走高德时**不能**再标 estimated=True。
    """
    try:
        from . import amap
        if not amap.has_key():
            return None
        r = amap.route(a["lat"], a["lon"], b["lat"], b["lon"], m,
                       alternatives=(m == "driving"))
        if not r.get("ok") or not r.get("routes"):
            return None
        return {"ok": True, "routes": r["routes"],
                "traffic_aware": bool(r.get("traffic_aware"))}
    except Exception:
        return None


def _best_and_reason(routes: list) -> tuple:
    """从多条候选里挑推荐的那条，并给出**基于真实数字**的理由。

    规则：时间优先；但若另一条只慢一点点（<8%）却明显更短（<95%），就选它 ——
    多绕十几公里省半分钟不划算。返回 (推荐下标, 理由文本)。
    """
    if not routes:
        return 0, ""
    if len(routes) == 1:
        return 0, "只有这一条可行路线"

    idx = min(range(len(routes)), key=lambda i: routes[i]["duration_s"])
    for i, r in enumerate(routes):
        if i == idx:
            continue
        if (r["duration_s"] <= routes[idx]["duration_s"] * 1.08
                and r["distance_m"] < routes[idx]["distance_m"] * 0.95):
            idx = i
            break

    b = routes[idx]
    other = min([r for i, r in enumerate(routes) if i != idx],
                key=lambda r: r["duration_s"])
    faster_min = (other["duration_s"] - b["duration_s"]) / 60.0    # >0 表示推荐的更快
    longer_km = (b["distance_m"] - other["distance_m"]) / 1000.0   # >0 表示推荐的更长

    bits = []
    if faster_min >= 0.5:
        bits.append("比另一条快约 %.0f 分钟" % faster_min)
    if longer_km <= -0.3:
        bits.append("距离还少约 %.1f 公里" % (-longer_km))
    elif longer_km >= 0.3:
        bits.append("代价是多走约 %.1f 公里" % longer_km)
    if not bits:
        bits.append("与另一条耗时接近，整体更顺")
    return idx, "；".join(bits)


def _mk_routes(raw_routes: list, mode: str, a: dict, b: dict) -> list:
    """把 OSRM 的多条候选整理成前端要的形状（统一 [lat,lon]、抽稀、算估算时间）。"""
    out = []
    for i, r in enumerate(raw_routes[:3]):        # 最多给 3 条，再多也没意义
        coords = ((r.get("geometry") or {}).get("coordinates") or [])
        pts = [[c[1], c[0]] for c in coords if len(c) >= 2]
        if len(pts) > 400:                        # 点太多前端画着卡，抽稀（首尾必留）
            step = len(pts) / 400.0
            keep = [pts[int(k * step)] for k in range(400)]
            if keep[-1] != pts[-1]:
                keep.append(pts[-1])
            pts = keep
        dist = float(r.get("distance") or 0)
        dur = float(r.get("duration") or 0)
        if mode == "foot":
            dur = dist / 1000.0 / _WALK_KMH * 3600.0
        elif mode == "bike":
            dur = dist / 1000.0 / _BIKE_KMH * 3600.0
        out.append({"idx": i, "distance_m": dist, "duration_s": dur, "points": pts})
    return out


def plan_route(origin: str, dest: str, mode: str = "driving", allow_net=None,
               want_weather: bool = False, city: str = ""):
    """规划两点之间的路线。origin/dest 传地名（会自动地理编码），也接受 "lat,lon"。

    返回里顶层保留 distance_m/duration_s/points（＝**推荐的那条**），
    另外加 routes:[…] 给出多条候选，每条带 recommended / reason。
    离线时：算过的路线直接重放；没算过的返回 ok=False + approx（直线距离，仅供参考）。
    city：限定城市，透传给地理编码（避免同名地点被解析到外省）。
    """
    m = {"驾车": "driving", "开车": "driving", "driving": "driving", "car": "driving",
         "步行": "foot", "走路": "foot", "walking": "foot", "foot": "foot",
         "骑行": "bike", "自行车": "bike", "cycling": "bike", "bike": "bike"}.get(
        str(mode or "").strip().lower(), "driving")
    net = online() if allow_net is None else bool(allow_net)
    key = "%s|%s|%s|%s" % (_source_tag(net), m, _ckey(origin), _ckey(dest))

    # ① 本地缓存：没过期直接用；过期了联网时重算
    hit = _load_cache(_ROUTE_FILE).get(key)
    if hit and hit.get("routes") and _fresh(hit.get("ts"), _TTL_ROUTE):
        return _serve_cached(hit, m, net, want_weather)

    a = geocode_one(origin, allow_net=net, city=city)
    b = geocode_one(dest, allow_net=net, city=city)
    if not a:
        return {"ok": False, "offline": not net, "error": "找不到起点「%s」" % origin}
    if not b:
        return {"ok": False, "offline": not net, "error": "找不到终点「%s」" % dest}

    # ② 联网算：**优先走高德**（真实步行/骑行路径 + 实时路况 + 备选方案），
    #    没配 key 或请求失败再回退 OSRM
    why = ""
    if net:
        _am = _route_amap(a, b, m)
        if _am:
            routes = _am["routes"]
            best, reason = _best_and_reason(routes)
            for rt in routes:
                rt.setdefault("tolls", 0.0)
                rt.setdefault("traffic_lights", 0)
                rt["recommended"] = (rt["idx"] == best)
                rt["reason"] = reason if rt["idx"] == best else ""
            cache_put(_ROUTE_FILE, key, {"ts": _now(), "routes": routes, "best": best,
                                         "source": "amap",
                                         # 起终点也要存：从缓存出结果时不能丢名字
                                         "from_name": a["name"], "from_lat": a["lat"],
                                         "from_lon": a["lon"],
                                         "to_name": b["name"], "to_lat": b["lat"],
                                         "to_lon": b["lon"]})
            res = _shape(routes, best, m, a, b, net, want_weather, amap_ok=True)
            res["traffic_aware"] = _am.get("traffic_aware")
            res["source"] = "amap"
            return res

        url = OSRM % ("driving", "%.6f,%.6f;%.6f,%.6f"
                      % (a["lon"], a["lat"], b["lon"], b["lat"]))
        # alternatives=true：中长途能给 2~3 条备选（实测广州塔→白云机场 2 条）；
        # 短途本来就只有一条路，返回 1 条也正常。
        url += "?overview=full&geometries=geojson&alternatives=true"
        try:
            d = _get_json(url, timeout=25)
            if d.get("code") == "Ok" and d.get("routes"):
                routes = _mk_routes(d["routes"], m, a, b)
                best, reason = _best_and_reason(routes)
                for r in routes:
                    r["recommended"] = (r["idx"] == best)
                    r["reason"] = reason if r["idx"] == best else ""
                cache_put(_ROUTE_FILE, key, {"ts": _now(), "routes": routes,
                                             "best": best,
                                             # 起终点也要存：从缓存出结果时不能丢名字
                                             "from_name": a["name"], "from_lat": a["lat"],
                                             "from_lon": a["lon"],
                                             "to_name": b["name"], "to_lat": b["lat"],
                                             "to_lon": b["lon"]})
                return _shape(routes, best, m, a, b, net, want_weather)
            why = str(d.get("code") or "无结果")
        except Exception as e:
            why = str(e)

    # ②b 联网没成功：过期的缓存也照用，标明它有多旧
    if hit and hit.get("routes"):
        return _serve_cached(hit, m, net, want_weather)

    # ③ 兜底：直线距离（明确标注不是实际道路）
    return _approx_route(a, b, m, net, why)


def _serve_cached(hit: dict, m: str, net: bool, want_weather: bool) -> dict:
    """用缓存里的路线出结果，标明缓存时间和是否已过期。"""
    routes = [dict(r) for r in (hit.get("routes") or [])]
    best = int(hit.get("best") or 0)
    frm = (routes[best] if routes else {})
    a = {"name": hit.get("from_name") or "", "lat": hit.get("from_lat"),
         "lon": hit.get("from_lon")}
    b = {"name": hit.get("to_name") or "", "lat": hit.get("to_lat"),
         "lon": hit.get("to_lon")}
    res = _shape(routes, best, m, a, b, net, want_weather,
                 amap_ok=(hit.get("source") == "amap"))
    res["from_cache"] = True
    res["cache_ts"] = hit.get("ts")
    res["stale"] = not _fresh(hit.get("ts"), _TTL_ROUTE)
    return res


def _shape(routes: list, best: int, m: str, a: dict, b: dict,
           net: bool, want_weather: bool, amap_ok: bool = False) -> dict:
    """统一拼装返回结构（缓存与实时走同一条路，避免两边字段不一致）。

    amap_ok=True 表示数据来自高德 —— 它的步行/骑行是**真实路径**，
    所以不再标 estimated，也不显示"时间系估算"那句说明。
    """
    b_rt = routes[best] if routes else {"distance_m": 0, "duration_s": 0, "points": []}
    est = (m in ("foot", "bike")) and not amap_ok
    res = {
        "ok": True, "mode": m, "routes": routes, "best": best,
        "estimated": est,
        "from": {"name": a.get("name"), "lat": a.get("lat"), "lon": a.get("lon")},
        "to": {"name": b.get("name"), "lat": b.get("lat"), "lon": b.get("lon")},
        # 顶层保留这三个，兼容老调用方
        "distance_m": b_rt["distance_m"], "duration_s": b_rt["duration_s"],
        "points": b_rt["points"],
    }
    if est:
        # ⚠️ 这段会直接显示在卡片上（前端只做转义、不渲染 markdown），所以别写星号
        res["note"] = ("免费路网服务只提供驾车路径，这里是驾车路线；"
                       "%s时间按 %.1f km/h 估算，仅供参考，不是真实步行/骑行路径。"
                       % ("步行" if m == "foot" else "骑行",
                          _WALK_KMH if m == "foot" else _BIKE_KMH))
    if want_weather and net:
        wf, wt = weather_at(a["lat"], a["lon"]), weather_at(b["lat"], b["lon"])
        if wf or wt:
            res["weather"] = {
                "from": {"name": a.get("name"), "now": wf.get("now") or {}},
                "to": {"name": b.get("name"),
                       "arrival": pick_hourly(wt, b_rt["duration_s"] / 60.0)},
            }
    return res


def _approx_route(a: dict, b: dict, m: str, net: bool, why: str = "") -> dict:
    """离线兜底：没有路网数据就算不出真路线，但直线距离和方位是**本地能算的**。

    ⚠️ 必须让上层知道这不是真路线（ok=False + approx + 说明文字），
       否则用户会以为"直线 6 公里"就是开车距离。
    """
    d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if not net:
        err = "离线模式下没有「%s → %s」的路线缓存" % (a["name"], b["name"])
    else:
        err = "路线服务暂时不可用%s" % ("（%s）" % why if why else "")
    return {
        "ok": False, "offline": not net, "mode": m, "error": err,
        "from": {"name": a["name"], "lat": a["lat"], "lon": a["lon"]},
        "to": {"name": b["name"], "lat": b["lat"], "lon": b["lon"]},
        "approx": {"distance_m": d,
                   "bearing": bearing_text(a["lat"], a["lon"], b["lat"], b["lon"])},
        "hint": "联网查一次这条路线，之后断网也能重放（会自动存到本地）。",
    }


def fmt_distance(m: float) -> str:
    return ("%.0f 米" % m) if m < 1000 else ("%.1f 公里" % (m / 1000.0))


def fmt_duration(s: float) -> str:
    s = int(s or 0)
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分钟" % round(s / 60.0)
    return "%d 小时 %d 分钟" % (s // 3600, round((s % 3600) / 60.0))


# ---------------------------------------------------------------- 瓦片（带本地缓存）
def _tile_path(z: int, x: int, y: int, src: str = "osm") -> str:
    """⚠️ 两套底图**必须分开存** —— 同一个 z/x/y 在 OSM 和高德下指的不是同一块地
    （坐标系差 50~500 米）。混着存会出现"一半瓦片是对的、一半整体偏移"。

    OSM 沿用老路径（`map_cache/<z>/<x>/<y>.png`）不动 —— 用户已经攒下来的
    离线瓦片不能作废。
    """
    if src == "amap":
        return os.path.join(cache_dir(), "amap", str(z), str(x), "%d.png" % y)
    return os.path.join(cache_dir(), str(z), str(x), "%d.png" % y)


def get_tile(z: int, x: int, y: int, allow_net=None, src: str = ""):
    """取一张瓦片：先看本地缓存，没有再上网取并缓存。返回 (bytes, from_cache)。

    离线模式下**绝不联网**：缓存里有就给，没有就 (None, False) ——
    前端据此显示"这块区域还没离线缓存"，而不是干等。
    """
    z, x, y = int(z), int(x), int(y)
    if not (0 <= z <= MAX_ZOOM):
        return None, False
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        return None, False
    src = src or tile_source(allow_net)
    p = _tile_path(z, x, y, src)
    old = None
    if os.path.exists(p) and os.path.getsize(p) > 0:
        try:
            with open(p, "rb") as f:
                old = f.read()
        except OSError:
            old = None
        # 没过期就直接用，连看都不看网络
        if old is not None and _fresh(os.path.getmtime(p), _TTL_TILE):
            return old, True

    # 离线模式**绝不联网**：过期的旧瓦片照样给（有总比白屏强）
    if not (online() if allow_net is None else bool(allow_net)):
        return (old, True) if old else (None, False)

    for tpl in (AMAP_TILE_MIRRORS if src == "amap" else TILE_MIRRORS):
        try:
            data = _get(tpl % (z, x, y), timeout=12)
        except Exception:
            continue
        # ⚠️ 明显是"空白瓦片"的就别缓存 —— 高德对越界/查不到的瓦片会回一张
        #    ~179 字节的纯色 PNG，看着像成功。要是把它按 60 天 TTL 存下来，
        #    那块地就**永远白着了**（实测踩过：整片地图空白，还查不出原因）。
        if data and data[:4] == b"\x89PNG":
            if len(data) < 300:
                continue
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            except OSError:
                pass
            return data, False
    # 一张都没下到：过期的旧瓦片也比没有强
    return (old, True) if old else (None, False)


def _lonlat_to_tile(lat: float, lon: float, z: int):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _tile_latlon(lat: float, lon: float, src: str):
    """把 WGS-84 的坐标换成"瓦片所在坐标系"的坐标。

    高德瓦片是 GCJ-02 的：拿 WGS-84 去算瓦片号，会正好错开半条街，
    预下载下来的图对不上点位。这一步不能省。
    """
    if src != "amap":
        return lat, lon
    try:
        from . import amap
        lng, la = amap.wgs84_to_gcj02(float(lon), float(lat))
        return la, lng
    except Exception:
        return lat, lon


def prefetch_route(points, zoom: int = 12, span: int = 2, max_tiles: int = 220,
                   allow_net=None):
    """把一条路线沿途的瓦片下到本地（之后离线也能看）。返回下载/命中统计。

    zoom 是中心层级，会一并缓存 zoom-1 / zoom-2 附近的瓦片（缩放时也有图）。
    ⚠️ 预下载本身就得联网：离线模式下直接拒绝，别假装跑了。
    """
    net = online() if allow_net is None else bool(allow_net)
    if not net:
        return {"requested": 0, "new": 0, "cached": 0, "failed": 0,
                "error": "离线模式下没法预下载，先把「联网」开关打开再试。"}
    src = tile_source(True)
    todo = set()
    zs = [z for z in (zoom, zoom - 1, zoom - 2) if 0 <= z <= MAX_ZOOM]
    # 路线点抽稀，别把每个点都算一遍
    step = max(1, len(points) // 120)
    for i in range(0, len(points), step):
        # ⚠️ 高德瓦片按 GCJ-02 编号，先转换再算瓦片号，否则下到的图对不上点位
        tlat, tlon = _tile_latlon(points[i][0], points[i][1], src)
        for z in zs:
            tx, ty = _lonlat_to_tile(tlat, tlon, z)
            for dx in range(-span, span + 1):
                for dy in range(-span, span + 1):
                    todo.add((z, tx + dx, ty + dy))
                    if len(todo) >= max_tiles:
                        break
                if len(todo) >= max_tiles:
                    break
            if len(todo) >= max_tiles:
                break
        if len(todo) >= max_tiles:
            break

    hit = new = fail = 0
    for (z, x, y) in sorted(todo):
        data, cached = get_tile(z, x, y, allow_net=True, src=src)
        if data is None:
            fail += 1
        elif cached:
            hit += 1
        else:
            new += 1
        time.sleep(0.03)          # 别把人家服务器打爆
    return {"requested": len(todo), "new": new, "cached": hit, "failed": fail,
            "source": src, "source_name": tile_source_text(src)}


def prefetch_area(center_lat: float, center_lon: float, zoom: int = 13, span: int = 2):
    """把某个地点周围的瓦片下到本地。"""
    return prefetch_route([[center_lat, center_lon]], zoom=zoom, span=span,
                          max_tiles=200)


def tile_url(z: int, x: int, y: int) -> str:
    return "/api/map/tile/%d/%d/%d.png" % (z, x, y)


def static_map_hint(lat: float, lon: float, zoom: int = 13) -> str:
    """给一个不用 JS 也能看的入口（OSM 网页版）。"""
    return ("https://www.openstreetmap.org/?mlat=%.5f&mlon=%.5f#map=%d/%.5f/%.5f"
            % (lat, lon, zoom, lat, lon))


# ---------------------------------------------------------------- 附近场所（Overpass）
# ⚠️ 为什么不能用 Photon 做"附近"：Photon 是**按名字搜**的 ——
#    搜「餐厅」它只会找**名字里带"餐厅"两个字**的店（实测返回的是几十上百公里外的
#    "餐厅""XX餐厅"），根本给不出"这一带所有餐厅"。按类别+半径查 POI 得用 Overpass。
# ⚠️ Overpass 会限流（实测连续几个请求就 HTTP 429），所以这里**必须缓存 + 节流**。
OVERPASS = "https://overpass-api.de/api/interpreter"
_NB_FILE = "nearby.json"
_TTL_NEARBY = 7 * 86400

# 类别关键词 → Overpass 标签。用户说什么词都能对上（中英文都收）
_CAT_TAGS = [
    (("餐厅", "吃饭", "吃的", "吃点", "下馆子", "饭店", "餐馆", "美食", "快餐",
      "restaurant", "food"),
     '["amenity"~"restaurant|fast_food|food_court"]'),
    (("咖啡", "咖啡馆", "cafe", "coffee"), '["amenity"="cafe"]'),
    (("便利", "小卖", "杂货", "便利店", "convenience"),
     '["shop"~"convenience|grocery|general"]'),
    (("购物中心", "商场", "商城", "百货", "综合体", "mall", "shopping"),
     '["shop"~"mall|department_store"]'),
    (("超市", "supermarket"), '["shop"~"supermarket|department_store"]'),
    (("药店", "药房", "pharmacy"), '["amenity"="pharmacy"]'),
    (("医院", "诊所", "卫生院", "hospital", "clinic"), '["amenity"~"hospital|clinic|doctors"]'),
    (("银行", "bank"), '["amenity"="bank"]'),
    (("取款", "atm", "取钱"), '["amenity"~"atm|bank"]'),
    (("加油", "加油站", "fuel"), '["amenity"="fuel"]'),
    (("充电", "充电桩", "charging"), '["amenity"="charging_station"]'),
    (("停车", "停车场", "parking"), '["amenity"="parking"]'),
    (("酒店", "宾馆", "旅馆", "住宿", "hotel"), '["tourism"~"hotel|hostel|guest_house|motel"]'),
    (("学校", "大学", "学院", "school", "university"), '["amenity"~"school|university|college"]'),
    (("公交", "巴士", "bus"), '["highway"="bus_stop"]'),
    (("地铁", "subway", "metro"), '["railway"="station"]["station"~"subway"]'),
    (("火车", "高铁", "车站", "railway"), '["railway"~"station|halt"]'),
    (("厕所", "洗手间", "卫生间", "toilet"), '["amenity"="toilets"]'),
    (("公园", "绿地", "park"), '["leisure"~"park|garden"]'),
    (("菜市场", "市场", "market"), '["amenity"="marketplace"]'),
    (("快递", "驿站", "邮局", "post"), '["amenity"~"post_office|post_depot"]'),
    (("理发", "美发", "hair"), '["shop"~"hairdresser|beauty"]'),
    (("五金", "建材", "hardware"), '["shop"~"hardware|doityourself"]'),
    (("服装", "衣", "clothes"), '["shop"~"clothes|shoes"]'),
    (("景点", "旅游", "attraction", "sight"), '["tourism"~"attraction|viewpoint|museum"]'),
    (("医院", "clinic"), '["amenity"~"hospital|clinic"]'),
]


def _cat_tag(category: str) -> tuple:
    """把用户说的类别翻译成 Overpass 标签。返回 (标签, 标准类别名) 或 (None, "")。

    标准类别名还会被当成**关键词发给高德**（高德的类型码太杂，用中文词更稳），
    所以每组第一个词要选"最适合当搜索词"的那个。
    """
    c = str(category or "").strip().lower()
    if not c:
        return None, ""
    for keys, tag in _CAT_TAGS:
        for k in keys:
            if k in c:
                return tag, keys[0]
    return None, ""


# 常见城市名（不带"市"字），用来从查询词里抠出"要在哪个城市找"。
# ⚠️ 为什么非要有这个：高德的 /place/text **必须带地区限定**才出结果 ——
#    实测纯关键词（哪怕加 citylimit=false）一律返回 0 条；
#    而它的 /geocode 是**按地址解析**的：查「天河城」会命中
#    "江西省南昌市进贤县天河城"（那边真有个叫天河城的村子），
#    广州那个正主反而出不来。所以"搜 POI"和"解析地址"是两条路，不能混。
_CITIES = [
    "北京", "上海", "天津", "重庆", "广州", "深圳", "珠海", "汕头", "佛山", "韶关",
    "湛江", "肇庆", "江门", "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远",
    "东莞", "中山", "潮州", "揭阳", "云浮",
    "杭州", "宁波", "温州", "嘉兴", "绍兴", "金华", "台州",
    "苏州", "无锡", "常州", "南通", "徐州", "南京",
    "合肥", "福州", "厦门", "泉州", "南昌", "赣州", "济南", "青岛", "烟台", "威海",
    "郑州", "洛阳", "武汉", "宜昌", "长沙", "株洲", "成都", "绵阳", "昆明", "大理",
    "贵阳", "南宁", "桂林", "柳州", "海口", "三亚", "西安", "兰州", "西宁", "银川",
    "乌鲁木齐", "呼和浩特", "包头", "拉萨", "沈阳", "大连", "长春", "吉林",
    "哈尔滨", "石家庄", "唐山", "太原", "大同",
]


def _split_city(q: str) -> tuple:
    """从查询词里抠出 (城市名, 剩余关键词)。

    ⚠️ 两个不能省的保护：
      ① 只在**开头**匹配，而且剩余部分得有 ≥2 个字 ——
         否则「广州塔」会被拆成 city=广州 + kw=塔，搜出来一堆别的塔；
      ② 剩下的部分不能是个**裸的通用词** —— 否则「汕头大学」会被拆成
         city=汕头 + kw=大学，搜出"汕头广播电视大学"这种。
         这种情况宁可整串当地址解析（高德对"汕头大学"是能解析的）。
    """
    s = str(q or "").strip()
    best = ""
    for c in _CITIES:
        for form in (c + "市", c):
            if s.startswith(form) and len(s) - len(form) >= 2:
                if len(form) > len(best):
                    best = form
                break
    if not best:
        return "", s
    kw = s[len(best):].strip()
    if kw in _GENERIC_SUFFIX:
        return "", s
    return best[:-1] if best.endswith("市") else best, kw


# 「城市名 + 这些词」不该被拆开当关键词搜（会搜出一堆别的同名机构）
_GENERIC_SUFFIX = {
    "大学", "中学", "小学", "学院", "学校", "医院", "公园", "车站", "机场",
    "广场", "大厦", "酒店", "银行", "市场", "政府", "火车站", "高铁站",
}


def _poi_info(t: dict) -> dict:
    """看这条记录有多少可用信息，回一句**人话说明**。

    ⚠️ 刻意**不输出分数、不输出星级** —— OSM 根本没有评分数据，
       给个数字出来一定会被当成口碑分（用户明确要求把评分完全去掉）。
       只回答"这条记录靠不靠谱、要不要先确认"。
    """
    n = 0
    if t.get("name"):
        n += 1
    if any(t.get(k) for k in ("addr:street", "addr:full", "addr:housenumber",
                              "addr:city", "addr:district", "addr:province")):
        n += 1
    if t.get("phone") or t.get("contact:phone"):
        n += 1
    if t.get("website") or t.get("contact:website"):
        n += 1
    if t.get("opening_hours"):
        n += 1
    if n >= 3:
        return {"info_note": "记录里地址、联系方式这类信息比较全，看着是正常营业的场所"}
    if n >= 2:
        return {"info_note": "记录信息不太全，出发前建议先确认一下"}
    return {"info_note": "记录里基本只有名字和坐标，可能是小摊小店、也可能已经关了，去之前最好先确认"}


def _overpass(q: str):
    """问 Overpass。

    ⚠️ 两个必踩的坑：
    1. **必须重试**。官方实例 `overpass-api.de` 负载很高，实测 504 Gateway Timeout
       和 429 Too Many Requests 都很常见 —— 一次失败不代表"没有结果"。
    2. headers 的值**必须是字符串**。上次传了个 dict，urllib 在 putheader 里抛
       `TypeError: expected string or bytes-like object, got 'dict'`，
       看着像网络故障，其实是自己写错，白白浪费一轮排查。

    备选实例实测情况（别再试一遍了）：
      · `overpass-api.de`            —— 唯一能出中国数据的，但经常 504/429
      · `overpass.osm.ch`            —— 通，但是**瑞士区域实例**，只有瑞士数据
      · `kumi.systems` / `mail.ru` / `private.coffee` —— 一律超时
    所以这里只打官方实例，靠重试 + 缓存扛住。
    """
    body = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(OVERPASS, data=body, headers={
                "User-Agent": _UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))       # 504/429 大多歇一下就好
    raise last


def _nearby_amap(lat, lon, cat, radius, limit):
    """走高德周边搜索。没配 key / 失败 → 返回 None，调用方自动回退 Overpass。"""
    try:
        from . import amap
        if not amap.has_key():
            return None
        r = amap.place_around(lat, lon, cat, radius=radius, offset=min(int(limit), 25))
        if not r.get("ok") or not r.get("items"):
            return None
        items = []
        for it in r["items"][:limit]:
            row = {
                "name": it.get("name") or "",
                "lat": it["lat"], "lon": it["lon"],
                "dist_m": int(it.get("dist_m") or 0),
                "kind": it.get("kind") or cat,
                "addr": it.get("addr") or "",
                "phone": it.get("phone") or "",
                "website": "",
                "hours": "",
                "info_note": "",
            }
            # 高德有**真实评分**（餐饮/酒店等类目）和人均消费 —— OSM 完全没有
            if it.get("rating"):
                row["rating"] = str(it["rating"])
            if it.get("cost"):
                row["cost"] = str(it["cost"])
            items.append(row)
        return {"ok": True, "category": cat, "radius": radius, "center": [lat, lon],
                "total": int(r.get("total") or len(items)), "items": items,
                "source": "amap",
                "note": "数据来自高德地图。"}
    except Exception:
        return None


def nearby(lat: float, lon: float, category: str, radius: int = 1500,
           limit: int = 20, allow_net=None) -> dict:
    """查某个坐标周围指定类别的场所（按距离排序）。

    返回 {"ok","items":[{name,lat,lon,dist_m,kind,addr,phone,website,hours,
                        info_note}],"category","radius","note"}
    ⚠️ **没有 score 字段** —— OSM 不提供评分，也不该拿别的数字冒充评分。
    """
    tag, cat = _cat_tag(category)
    if not tag:
        return {"ok": False, "error": "不认识这个类别：「%s」。可以试：餐厅 / 便利店 / "
                                     "超市 / 药店 / 医院 / 银行 / 加油站 / 停车场 / "
                                     "酒店 / 学校 / 公交站 / 公园 / 厕所…" % category}
    net = online() if allow_net is None else bool(allow_net)
    # 高德最大支持 50000 米；「整个城市里有什么」这种问法需要大半径
    radius = max(100, min(50000, int(radius or 1500)))
    limit = max(1, min(60, int(limit or 20)))
    # ⚠️ 缓存键必须带**数据源** —— 高德和 OSM 的结果质量差很多，
    # 共用一个键会出这种事（实测踩过）：刚配好高德 key，看到的却还是旧的 OSM 数据。
    key = "%s|%s|%.3f,%.3f|%d" % (_source_tag(net), cat, lat, lon, radius)

    # 先看缓存（Overpass 会 429，缓存是必须的，不是优化）
    hit = _load_cache(_NB_FILE).get(key)
    if hit and hit.get("items") and _fresh(hit.get("ts"), _TTL_NEARBY):
        res = dict(hit["items"])
        res["from_cache"] = True
        res["cache_ts"] = hit.get("ts")
        return res
    if not net:
        if hit and hit.get("items"):
            res = dict(hit["items"])
            res.update(from_cache=True, cache_ts=hit.get("ts"), stale=True)
            return res
        return {"ok": False, "offline": True,
                "error": "离线模式下没查过这一带的「%s」，查不到。"
                         "联网问一次之后就会存到本地，下次离线也能看。" % category}

    # ---- 联网：**优先走高德**（配了 key 的话）----
    # 高德的 POI 库比 OSM 强太多：实测汕头大学 1.5km 内，
    # OSM 只录到 3 家餐厅、便利店 **0 家**；高德分别有 7 家和 11 家，
    # 而且带**真实评分**和人均消费 —— 这正是用户要的东西。
    _am = _nearby_amap(lat, lon, cat, radius, limit)
    if _am:
        cache_put(_NB_FILE, key, {"ts": _now(), "items": _am})
        return _am

    # ---- 回退 Overpass（没配 key / 高德失败）----
    q = ('[out:json][timeout:30];('
         'node(around:%d,%.6f,%.6f)%s;'
         'way(around:%d,%.6f,%.6f)%s;'
         'relation(around:%d,%.6f,%.6f)%s;'
         ');out center %d;'
         % (radius, lat, lon, tag, radius, lat, lon, tag,
            radius, lat, lon, tag, limit * 2))
    try:
        d = _overpass(q)
    except Exception as e:
        # 429 很常见，把话说清楚，别让用户以为是"没有结果"
        msg = str(e)
        if "429" in msg:
            msg = "查询太频繁被限流了，歇十几秒再试"
        if hit and hit.get("items"):
            res = dict(hit["items"])
            res.update(from_cache=True, cache_ts=hit.get("ts"), stale=True)
            return res
        return {"ok": False, "error": "附近查询失败：%s" % msg}

    items = []
    for el in (d.get("elements") or []):
        t = el.get("tags") or {}
        la = el.get("lat") or (el.get("center") or {}).get("lat")
        lo = el.get("lon") or (el.get("center") or {}).get("lon")
        if la is None or lo is None:
            continue
        dd = haversine(lat, lon, float(la), float(lo))
        if dd > radius:
            continue
        info = _poi_info(t)
        addr = "".join(str(t.get(k) or "") for k in
                       ("addr:province", "addr:city", "addr:district",
                        "addr:street", "addr:housenumber"))
        items.append({
            "name": t.get("name") or "",
            "lat": round(float(la), 6), "lon": round(float(lo), 6),
            "dist_m": int(dd), "kind": cat,
            "addr": addr, "phone": t.get("phone") or t.get("contact:phone") or "",
            "website": t.get("website") or t.get("contact:website") or "",
            "hours": t.get("opening_hours") or "",
            "brand": t.get("brand") or "",
            "info_note": info["info_note"],
        })
    # 只按距离排（没有评分可用，也不该拿记录完整度冒充评分去排序）
    items.sort(key=lambda x: x["dist_m"])
    res = {"ok": True, "category": cat, "radius": radius, "center": [lat, lon],
           "total": len(items), "items": items[:limit],
           "note": "数据来自 OpenStreetMap（志愿者测绘）。中国的小微店铺覆盖很稀疏，"
                   "「查不到」不等于「没有」。这里**没有评分数据**（OSM 不提供评分），"
                   "只给距离和「这条记录全不全」。"}
    cache_put(_NB_FILE, key, {"ts": _now(), "items": res})
    return res


# ---------------------------------------------------------------- 交通方式对比与建议
_MODE_CN = {"driving": "驾车", "foot": "步行", "bike": "骑行"}


def suggest_mode(distance_m: float) -> tuple:
    """按距离给出更合适的出行方式 + 原因。返回 (mode, 理由)。"""
    d = float(distance_m or 0) / 1000.0
    if d <= 1.2:
        return "foot", ("才 %.1f 公里，走路最快，还不用找车位" % d) if d > 0.05 else "距离很近，走过去就行"
    if d <= 4.5:
        return "bike", ("%.1f 公里这个距离骑车最划算：比走路快得多，又比开车灵活"
                        "（市区找车位、堵车都省了）" % d)
    if d <= 30:
        return "driving", "%.1f 公里，开车明显更省时间，骑车要花好几倍功夫" % d
    return "driving", "%.1f 公里属于长途，只能开车或坐公共交通" % d


def compare_modes(origin: str, dest: str, allow_net=None) -> dict:
    """把三种出行方式都算一遍（用同一套路网数据），供"该选哪种"的对比。

    ⚠️ 三种方式的**路径都来自驾车路网**（免费 OSRM 只跑 car profile），
       步行/骑行的时间是按速度换算的估算值 —— 结果里带 estimated 标记，
       提交给用户时必须说明，不能让他以为那是真实步行路径。
    """
    out, dist = {}, 0.0
    for m in ("driving", "foot", "bike"):
        r = plan_route(origin, dest, m, allow_net=allow_net)
        if r.get("ok"):
            out[m] = {"distance_m": r["distance_m"], "duration_s": r["duration_s"],
                      "estimated": bool(r.get("estimated"))}
            dist = dist or r["distance_m"]
    if not out:
        return {"ok": False, "error": "三种方式都没算出来（可能是地点没找到或网不通）"}
    best, why = suggest_mode(dist)
    return {"ok": True, "modes": out, "distance_m": dist,
            "suggest": best, "suggest_cn": _MODE_CN.get(best, best),
            "suggest_reason": why,
            "note": "步行/骑行的路径也取自驾车路网，时间按速度估算，仅供参考。"}
