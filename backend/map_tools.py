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
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.endswith(".png"):
                n += 1
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
            "places": len(geo), "routes": len(rts),
            "builtin": builtin_count(),
            "newest_ts": newest,
            "online": online()}


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


# ---------------------------------------------------------------- 结果缓存
# 为什么要它：离线模式能干什么，全看这里攒了多少东西。
# 联网查过的地名、算过的路线都留在本地，之后断网还能重放。
_GEO_FILE = "geocode.json"
_ROUTE_FILE = "routes.json"


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


def search_place(query: str, limit: int = 5, allow_net=None) -> list:
    """按名字找地点，返回 [{name, lat, lon, addr, kind}]。

    三级顺序：① 本地缓存 → ② 联网查 Photon（查到顺手存下）→ ③ 内置常用地名表。
    离线时（allow_net=False，或「联网」开关关着）①③ 照样能用，中间那步跳过。
    返回项里带 from_cache / cache_ts 便于上层告诉用户"这是本地数据"。
    """
    q = str(query or "").strip()
    if not q:
        return []
    limit = max(1, min(10, int(limit or 5)))
    net = online() if allow_net is None else bool(allow_net)

    # ① 本地缓存：以前查过一次，之后断网也认
    hit = _load_cache(_GEO_FILE).get(_ckey(q))
    if hit and hit.get("results"):
        return [dict(r, from_cache=True, cache_ts=hit.get("ts"))
                for r in hit["results"][:limit]]

    # ② 联网查 ⚠️ 不要带 &lang=zh —— 带了中文查询会全部返回空
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
                cache_put(_GEO_FILE, q, {"ts": _now(), "results": out})
                return out[:limit]
        except Exception:
            pass      # 网断了 / Photon 挂了 → 继续往下走兜底

    # ③ 兜底：内置常用地名表（离线时主力）
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


def geocode_one(query: str, allow_net=None):
    """只要最匹配的一个，返回 dict 或 None。"""
    c = _parse_latlon(query)
    if c:
        return c
    r = search_place(query, limit=1, allow_net=allow_net)
    if not r:
        return None
    return r[0]


# ---------------------------------------------------------------- 路线
def _mk_route(r: dict, m: str, a: dict, b: dict) -> dict:
    """把 OSRM 的返回整理成前端要的形状。"""
    coords = ((r.get("geometry") or {}).get("coordinates") or [])
    # OSRM 给的是 [lon,lat]，统一成 [lat,lon] 交给前端
    pts = [[c[1], c[0]] for c in coords if len(c) >= 2]
    # 点太多前端画着卡，抽稀到 400 个以内（首尾必留）
    if len(pts) > 400:
        step = len(pts) / 400.0
        keep = [pts[int(i * step)] for i in range(400)]
        if keep[-1] != pts[-1]:
            keep.append(pts[-1])
        pts = keep
    return {"ok": True, "mode": m,
            "from": {"name": a["name"], "lat": a["lat"], "lon": a["lon"]},
            "to": {"name": b["name"], "lat": b["lat"], "lon": b["lon"]},
            "distance_m": float(r.get("distance") or 0),
            "duration_s": float(r.get("duration") or 0),
            "points": pts}


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


def plan_route(origin: str, dest: str, mode: str = "driving", allow_net=None):
    """规划两点之间的路线。origin/dest 传地名（会自动地理编码），也接受 "lat,lon"。

    返回 {"ok","from","to","distance_m","duration_s","points":[[lat,lon]…],"mode"}。
    离线时：算过的路线直接重放；没算过的返回 ok=False + approx（直线距离，仅供参考）。
    """
    m = {"驾车": "driving", "开车": "driving", "driving": "driving", "car": "driving",
         "步行": "foot", "走路": "foot", "walking": "foot", "foot": "foot",
         "骑行": "bike", "自行车": "bike", "cycling": "bike", "bike": "bike"}.get(
        str(mode or "").strip().lower(), "driving")
    net = online() if allow_net is None else bool(allow_net)
    key = "%s|%s|%s" % (m, _ckey(origin), _ckey(dest))

    # ① 本地缓存：以前联网算过这条，断网也能重放
    hit = _load_cache(_ROUTE_FILE).get(key)
    if hit and hit.get("route"):
        r = dict(hit["route"])
        r["from_cache"] = True
        r["cache_ts"] = hit.get("ts")
        return r

    a = geocode_one(origin, allow_net=net)
    b = geocode_one(dest, allow_net=net)
    if not a:
        return {"ok": False, "offline": not net, "error": "找不到起点「%s」" % origin}
    if not b:
        return {"ok": False, "offline": not net, "error": "找不到终点「%s」" % dest}

    # ② 联网算
    why = ""
    if net:
        url = OSRM % (m, "%.6f,%.6f;%.6f,%.6f" % (a["lon"], a["lat"], b["lon"], b["lat"]))
        url += "?overview=full&geometries=geojson"
        try:
            d = _get_json(url, timeout=25)
            if d.get("code") == "Ok" and d.get("routes"):
                r = _mk_route(d["routes"][0], m, a, b)
                cache_put(_ROUTE_FILE, key, {"ts": _now(), "route": r})
                return r
            why = str(d.get("code") or "无结果")
        except Exception as e:
            why = str(e)

    # ③ 兜底：直线距离（明确标注不是实际道路）
    return _approx_route(a, b, m, net, why)


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
def _tile_path(z: int, x: int, y: int) -> str:
    return os.path.join(cache_dir(), str(z), str(x), "%d.png" % y)


def get_tile(z: int, x: int, y: int, allow_net=None):
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
    p = _tile_path(z, x, y)
    if os.path.exists(p) and os.path.getsize(p) > 0:
        try:
            with open(p, "rb") as f:
                return f.read(), True
        except OSError:
            pass
    if not (online() if allow_net is None else bool(allow_net)):
        return None, False
    for tpl in TILE_MIRRORS:
        try:
            data = _get(tpl % (z, x, y), timeout=12)
        except Exception:
            continue
        if data and data[:4] == b"\x89PNG":
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            except OSError:
                pass
            return data, False
    return None, False


def _lonlat_to_tile(lat: float, lon: float, z: int):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


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
    todo = set()
    zs = [z for z in (zoom, zoom - 1, zoom - 2) if 0 <= z <= MAX_ZOOM]
    # 路线点抽稀，别把每个点都算一遍
    step = max(1, len(points) // 120)
    for i in range(0, len(points), step):
        lat, lon = points[i][0], points[i][1]
        for z in zs:
            tx, ty = _lonlat_to_tile(lat, lon, z)
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
        data, cached = get_tile(z, x, y)
        if data is None:
            fail += 1
        elif cached:
            hit += 1
        else:
            new += 1
        time.sleep(0.03)          # 别把人家服务器打爆
    return {"requested": len(todo), "new": new, "cached": hit, "failed": fail}


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
