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
    return {"tiles": n, "bytes": size, "dir": d}


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


def search_place(query: str, limit: int = 5) -> list:
    """按名字找地点，返回 [{name, lat, lon, addr, kind}]（失败返回空列表）。"""
    q = str(query or "").strip()
    if not q:
        return []
    # ⚠️ 不要带 &lang=zh —— 带了中文查询会全部返回空
    url = "%s?q=%s&limit=%d" % (PHOTON, urllib.parse.quote(q), max(1, min(10, limit)))
    try:
        d = _get_json(url)
    except Exception:
        return []
    out = []
    for f in (d.get("features") or []):
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
            p = f.get("properties") or {}
            out.append({"name": p.get("name") or q, "lat": float(lat), "lon": float(lon),
                        "addr": _addr(p), "kind": p.get("osm_value") or p.get("osm_key") or ""})
        except Exception:
            continue
    return out


def geocode_one(query: str):
    """只要最匹配的一个，返回 (name, lat, lon) 或 None。"""
    r = search_place(query, limit=1)
    if not r:
        return None
    return r[0]


# ---------------------------------------------------------------- 路线
def plan_route(origin: str, dest: str, mode: str = "driving"):
    """规划两点之间的路线。origin/dest 传地名（会先地理编码）。

    返回 {"ok","from","to","distance_m","duration_s","points":[[lat,lon]…],"mode"}。
    """
    m = {"驾车": "driving", "开车": "driving", "driving": "driving", "car": "driving",
         "步行": "foot", "走路": "foot", "walking": "foot", "foot": "foot",
         "骑行": "bike", "自行车": "bike", "cycling": "bike", "bike": "bike"}.get(
        str(mode or "").strip().lower(), "driving")
    a = geocode_one(origin)
    b = geocode_one(dest)
    if not a:
        return {"ok": False, "error": "找不到起点「%s」" % origin}
    if not b:
        return {"ok": False, "error": "找不到终点「%s」" % dest}
    url = OSRM % (m, "%.6f,%.6f;%.6f,%.6f" % (a["lon"], a["lat"], b["lon"], b["lat"]))
    url += "?overview=full&geometries=geojson"
    try:
        d = _get_json(url, timeout=25)
    except Exception as e:
        return {"ok": False, "error": "路线服务暂时不可用（%s）" % e}
    if d.get("code") != "Ok" or not d.get("routes"):
        return {"ok": False, "error": "没能规划出路线（%s）" % d.get("code")}
    r = d["routes"][0]
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


def get_tile(z: int, x: int, y: int):
    """取一张瓦片：先看本地缓存，没有再上网取并缓存。返回 (bytes, from_cache)。"""
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


def prefetch_route(points, zoom: int = 12, span: int = 2, max_tiles: int = 220):
    """把一条路线沿途的瓦片下到本地（离线也能看）。返回下载/命中统计。

    zoom 是中心层级，会一并缓存 zoom-1 / zoom-2 附近的瓦片（缩放时也有图）。
    """
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
