# -*- coding: utf-8 -*-
"""高德地图 Web 服务客户端（联网模式下优先用它）。

为什么要有这个模块：OpenStreetMap 在中国实在不够用 ——
实测汕头大学 1.5 公里内只录到 3 家餐厅、搜「便利店 汕头」返回 **0 条**；
高德同样一次查询返回 **600 条**，还带电话、营业时间，餐饮类还有**评分**。
实时路况、公交换乘、真实步行/骑行路径也只有高德有。

⚠️⚠️ 坐标系（最容易出错的地方）
    高德用 **GCJ-02**（火星坐标），OSM / GPS 用 **WGS-84**，
    混用会有 **50~500 米**偏移（表现就是"点和路对不上"）。
    本项目底图是 OSM，所以约定：
        · **内部一律 WGS-84**
        · 发给高德前 → gcj（`wgs84_to_gcj02`）
        · 收到高德的坐标 → wgs（`gcj02_to_wgs84`）
    另外高德的 location 是 **"经度,纬度"**（先经后纬），别和 [lat, lon] 搞反。

⚠️ 额度：个人开发者每日免费额度有限（各接口约 5000 次/日），
    所以缓存是必须的，不能每个请求都打高德。
"""
from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request

AMAP = "https://restapi.amap.com/v3"
AMAP_V4 = "https://restapi.amap.com/v4"      # 骑行在这套（v3 没有骑行接口）
_UA = "local-multimodal-assistant/1.0"


# ---------------------------------------------------------------- key
def key() -> str:
    """读配置里的高德 key。没配就返回空串 —— 调用方据此**自动回退**到 OpenStreetMap。"""
    try:
        from . import config
        return str(config.load_config().get("amap_key") or "").strip()
    except Exception:
        return ""


def has_key() -> bool:
    return bool(key())


# ---------------------------------------------------------------- 坐标转换
_A = 6378245.0                       # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    """境外不做偏移（高德对境外坐标不加密）。"""
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _tf_lat(x: float, y: float) -> float:
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tf_lng(x: float, y: float) -> float:
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lng: float, lat: float):
    """发给高德之前用这个。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _tf_lat(lng - 105.0, lat - 35.0)
    dlng = _tf_lng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = math.sin(rad)
    magic = 1 - _EE * magic * magic
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sq) * math.pi)
    dlng = (dlng * 180.0) / (_A / sq * math.cos(rad) * math.pi)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float):
    """收到高德坐标后转回来。一次反向差分近似，精度 1~2 米，打点/画线够用。"""
    if _out_of_china(lng, lat):
        return lng, lat
    glng, glat = wgs84_to_gcj02(lng, lat)
    return lng * 2 - glng, lat * 2 - glat


# ---------------------------------------------------------------- HTTP
def _get_url(url: str, timeout: int = 20):
    """底层请求。失败一律返回 None（调用方回退 OSM），不抛异常。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    # v3 用 status=1，v4 用 errcode=0 —— 两套都要认
    if d.get("errcode") is not None:
        return d if str(d.get("errcode")) == "0" else None
    return d if str(d.get("status")) == "1" else None


def _get(path: str, timeout: int = 20, **params):
    """调 v3 接口。没 key / 失败都返回 None。"""
    k = key()
    if not k:
        return None
    params["key"] = k
    params["output"] = "json"
    url = AMAP + path + "?" + urllib.parse.urlencode(
        {a: b for a, b in params.items() if b not in (None, "")})
    return _get_url(url, timeout)


def _get_v4(path: str, timeout: int = 20, **params):
    """调 v4 接口 —— 高德的**骑行**只在 v4（v3 没这个接口），
    而且返回体结构不一样（`{errcode, data:{paths}}`），别拿 v3 的解析套。"""
    k = key()
    if not k:
        return None
    params["key"] = k
    url = AMAP_V4 + path + "?" + urllib.parse.urlencode(
        {a: b for a, b in params.items() if b not in (None, "")})
    return _get_url(url, timeout)


def _loc(s: str):
    """高德的 "经度,纬度" → (lat, lon)，**已转成 WGS-84**。"""
    try:
        lng, lat = str(s).split(",")[:2]
        wlng, wlat = gcj02_to_wgs84(float(lng), float(lat))
        return round(wlat, 6), round(wlng, 6)
    except Exception:
        return None


def to_gcj_str(lat: float, lon: float) -> str:
    """WGS-84 的 (lat, lon) → 高德要的 "经度,纬度"。"""
    lng, la = wgs84_to_gcj02(float(lon), float(lat))
    return "%.6f,%.6f" % (lng, la)


# ---------------------------------------------------------------- 地理编码
def geocode(address: str, city: str = "") -> list:
    """地名 → 坐标。返回 [{name, lat, lon, addr, adcode, city}]（坐标为 WGS-84）。"""
    d = _get("/geocode/geo", address=address, city=city)
    if not d:
        return []
    out = []
    for g in (d.get("geocodes") or []):
        c = _loc(g.get("location"))
        if not c:
            continue
        out.append({"name": g.get("formatted_address") or address,
                    "lat": c[0], "lon": c[1],
                    "addr": g.get("formatted_address") or "",
                    "adcode": g.get("adcode") or "",
                    "city": g.get("city") or "",
                    "kind": "amap"})
    return out


def regeo(lat: float, lon: float) -> dict:
    """坐标 → 人话地址。"""
    d = _get("/geocode/regeo", location=to_gcj_str(lat, lon),
             extensions="base", radius=1000)
    if not d:
        return {}
    rc = d.get("regeocode") or {}
    comp = rc.get("addressComponent") or {}
    return {"addr": rc.get("formatted_address") or "",
            "city": comp.get("city") or comp.get("province") or "",
            "district": comp.get("district") or "",
            "adcode": comp.get("adcode") or ""}


# ---------------------------------------------------------------- POI
def _poi(el: dict, origin=None) -> dict:
    """把一条高德 POI 整理成统一结构（坐标已转 WGS-84）。"""
    c = _loc(el.get("location"))
    if not c:
        return {}
    biz = el.get("biz_ext") or {}
    if not isinstance(biz, dict):        # 高德有时返回 [] 而不是 {}
        biz = {}
    addr = el.get("address")
    if not isinstance(addr, str):        # 偶尔是 [] —— 直接显示会变成 "[]"
        addr = ""
    tel = el.get("tel")
    if not isinstance(tel, str):
        tel = ""
    item = {
        "name": el.get("name") or "",
        "lat": c[0], "lon": c[1],
        "addr": addr,
        "kind": (el.get("type") or "").split(";")[-1],
        "type_full": el.get("type") or "",
        "phone": tel,
        "city": el.get("cityname") or "",
        "district": el.get("adname") or "",
        "adcode": el.get("adcode") or "",
        "id": el.get("id") or "",
    }
    # 评分/人均：只有部分类目（餐饮、酒店等）才给，没有就不放这个字段
    rating = biz.get("rating")
    if rating:
        item["rating"] = str(rating)
    cost = biz.get("cost")
    if cost:
        item["cost"] = str(cost)
    if origin:
        item["dist_m"] = int(math.hypot((c[0] - origin[0]) * 111000.0,
                                        (c[1] - origin[1]) * 111000.0 * 0.85))
    return item


def place_text(keywords: str, city: str = "", page: int = 1, offset: int = 20) -> dict:
    """关键词搜 POI（全国 / 指定城市）。"""
    d = _get("/place/text", keywords=keywords, city=city, page=page,
             offset=min(int(offset or 20), 25), extensions="all")
    if not d:
        return {"ok": False, "items": []}
    items = [x for x in (_poi(p) for p in (d.get("pois") or [])) if x]
    items.sort(key=lambda x: -float(x.get("rating") or 0))   # 有评分的排前面
    return {"ok": True, "items": items, "total": int(d.get("count") or len(items))}


def place_around(lat: float, lon: float, keywords: str = "", radius: int = 1500,
                 offset: int = 25) -> dict:
    """**周边搜索** —— 这就是「附近有什么」。按距离排序。"""
    d = _get("/place/around", location=to_gcj_str(lat, lon), keywords=keywords,
             radius=int(radius), offset=min(int(offset or 25), 25),
             sortrule="distance", extensions="all")
    if not d:
        return {"ok": False, "items": []}
    items = [x for x in (_poi(p, origin=(lat, lon)) for p in (d.get("pois") or [])) if x]
    items.sort(key=lambda x: x.get("dist_m", 9e9))
    return {"ok": True, "items": items, "total": int(d.get("count") or len(items))}


# ---------------------------------------------------------------- 路径规划
_MODE_PATH = {"driving": "/direction/driving", "foot": "/direction/walking",
              "bike": "/direction/bicycling"}

# 驾车策略。默认 32 = 综合推荐，**会参考实时路况**；想明确避堵传 4
_STRATEGY_CN = {0: "速度优先", 1: "费用优先", 2: "距离优先", 3: "不走快速路",
                4: "躲避拥堵", 32: "综合推荐", 33: "避免收费",
                34: "躲避拥堵且避免收费", 35: "多策略"}


def _polyline(s: str) -> list:
    """"lng,lat;lng,lat;…" → [[lat,lon],…]（转 WGS-84 + 抽稀到 400 点内）。"""
    pts = []
    for seg in str(s or "").split(";"):
        if not seg:
            continue
        try:
            lng, lat = seg.split(",")[:2]
            wlng, wlat = gcj02_to_wgs84(float(lng), float(lat))
            pts.append([round(wlat, 6), round(wlng, 6)])
        except Exception:
            continue
    if len(pts) > 400:
        step = len(pts) / 400.0
        keep = [pts[int(i * step)] for i in range(400)]
        if keep[-1] != pts[-1]:
            keep.append(pts[-1])
        pts = keep
    return pts


def _route_bike(olat, olon, dlat, dlon) -> dict:
    """骑行 —— 高德**只在这个 v4 接口**提供，返回结构也和 v3 不同。"""
    d = _get_v4("/direction/bicycling", timeout=25,
                origin=to_gcj_str(olat, olon), destination=to_gcj_str(dlat, dlon))
    if not d:
        return {"ok": False, "error": "高德骑行规划请求失败"}
    paths = (d.get("data") or {}).get("paths") or []
    if not paths:
        return {"ok": False, "error": "高德没给出骑行路线"}
    out = []
    for i, p in enumerate(paths[:3]):
        pts = []
        for st in (p.get("steps") or []):
            pts.extend(_polyline(st.get("polyline")))
        if len(pts) < 2:
            continue
        out.append({"idx": i, "distance_m": float(p.get("distance") or 0),
                    "duration_s": float(p.get("duration") or 0), "points": pts,
                    "tolls": 0.0, "traffic_lights": 0, "strategy": "骑行"})
    if not out:
        return {"ok": False, "error": "骑行路线没有可用路径点"}
    return {"ok": True, "routes": out, "mode": "bike", "traffic_aware": False}


def route(origin_lat, origin_lon, dest_lat, dest_lon, mode: str = "driving",
          strategy: int = 32, alternatives: bool = False) -> dict:
    """路径规划。

    返回 {"ok","routes":[{distance_m,duration_s,points,tolls,traffic_lights,strategy}],
          "mode","traffic_aware"}

    · mode: driving / foot / bike
    · **驾车的 duration 是高德按实时路况算的**，这是我们之前完全没有的东西
    · 步行/骑行是**真实路径**（不像免费 OSRM 只能给驾车路网再拿速度估算）
    """
    if mode == "bike":
        return _route_bike(origin_lat, origin_lon, dest_lat, dest_lon)
    path = _MODE_PATH.get(mode)
    if not path:
        return {"ok": False, "error": "高德不支持这种出行方式：%s" % mode}
    params = {"origin": to_gcj_str(origin_lat, origin_lon),
              "destination": to_gcj_str(dest_lat, dest_lon),
              "extensions": "all"}
    if mode == "driving":
        params["strategy"] = int(strategy)
        if alternatives:
            params["alternative_route"] = 1     # 请求备选路线
    d = _get(path, timeout=25, **params)
    if not d:
        return {"ok": False, "error": "高德路径规划请求失败"}
    route = d.get("route") or {}
    paths = route.get("paths") or []
    if not paths:
        return {"ok": False, "error": route.get("info") or "高德没给出可行路线"}

    out = []
    for i, p in enumerate(paths[:3]):
        pts = []
        for st in (p.get("steps") or []):
            pts.extend(_polyline(st.get("polyline")))
        if len(pts) < 2:
            continue
        out.append({
            "idx": i,
            "distance_m": float(p.get("distance") or 0),
            "duration_s": float(p.get("duration") or 0),
            "points": pts,
            "tolls": float(p.get("tolls") or 0),
            "traffic_lights": int(p.get("traffic_lights") or 0),
            # ⚠️ 高德返回的 strategy 是**中文字符串**（如"速度最快"），不是数字 ——
            #    直接 int() 会 ValueError，之前就栽在这。返回什么就用什么。
            "strategy": str(p.get("strategy") or _STRATEGY_CN.get(int(strategy), "")),
        })
    if not out:
        return {"ok": False, "error": "高德返回的路线没有可用路径点"}
    return {"ok": True, "routes": out, "mode": mode,
            "traffic_aware": mode == "driving"}


def transit(origin_lat, origin_lon, dest_lat, dest_lon,
            city: str = "", cityd: str = "") -> dict:
    """**公交换乘方案** —— 这是 OSM 完全给不出的能力。

    返回 {"ok","plans":[{duration_s,cost,walking_m,transfers,segments,points}]}
    """
    params = {"origin": to_gcj_str(origin_lat, origin_lon),
              "destination": to_gcj_str(dest_lat, dest_lon),
              "extensions": "all", "strategy": 0}
    if city:
        params["city"] = city
    if cityd:
        params["cityd"] = cityd
    d = _get("/direction/transit/integrated", timeout=30, **params)
    if not d:
        return {"ok": False, "error": "公交换乘查询失败（可能需要城市名，或额度用尽）"}
    route = d.get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        return {"ok": False, "error": route.get("info") or "没找到公交方案"}

    plans = []
    for t in transits[:3]:
        segs, pts = [], []
        for seg in (t.get("segments") or []):
            walk = seg.get("walking") or {}
            wsteps = walk.get("steps") or []
            wdist = float(walk.get("distance") or 0)
            if wdist > 0:
                segs.append({"type": "walk", "line": "",
                             "from": ((wsteps[0].get("instruction") or "") if wsteps else ""),
                             "to": "", "distance_m": int(wdist)})
            for st in wsteps:
                pts.extend(_polyline(st.get("polyline")))
            bus = seg.get("bus") or {}
            buses = bus.get("buslines") or []
            if buses:
                line = buses[0]
                segs.append({
                    "type": "bus",
                    "line": re.sub(r"[（(].*?[)）]", "", line.get("name") or ""),
                    "from": (line.get("departure_stop") or {}).get("name") or "",
                    "to": (line.get("arrival_stop") or {}).get("name") or "",
                    # ⚠️ 同一段高德常给好几条等价线路 —— 那是**可选**，不是"要坐 5 趟车"。
                    #    之前把每条都当成一段，算出来换乘 5 次，其实是 1 次。
                    "alts": [re.sub(r"[（(].*?[)）]", "", b.get("name") or "")
                             for b in buses[1:4]],
                    "via": line.get("via_num") or "",
                    "distance_m": int(float(line.get("distance") or 0)),
                })
                pts.extend(_polyline(line.get("polyline")))
        plans.append({
            # 高德公交返回的 duration **单位就是秒**（之前多乘了 60，算出"60 小时"）
            "duration_s": float(t.get("duration") or 0),
            "cost": float(t.get("cost") or 0),
            "walking_m": int(float(t.get("walking_distance") or 0)),
            "transfers": max(0, len([x for x in segs if x["type"] == "bus"]) - 1),
            "segments": segs,
            "points": pts,
        })
    plans.sort(key=lambda p: p["duration_s"])
    return {"ok": True, "plans": plans}


# ---------------------------------------------------------------- 天气
def weather(adcode: str) -> dict:
    """按**行政区编码**查天气（高德要 adcode，不是城市名）。"""
    if not adcode:
        return {"ok": False, "error": "缺少行政区编码"}
    d = _get("/weather/weatherInfo", city=str(adcode), extensions="all")
    if not d:
        return {"ok": False, "error": "天气查询失败"}
    casts = d.get("forecasts") or []
    if not casts:
        return {"ok": False, "error": "没拿到天气数据"}
    c = casts[0]
    return {"ok": True, "city": c.get("city") or "",
            "casts": [{"date": x.get("date"), "week": x.get("week"),
                       "day_weather": x.get("dayweather"),
                       "night_weather": x.get("nightweather"),
                       "day_temp": x.get("daytemp"), "night_temp": x.get("nighttemp"),
                       "wind": (x.get("daywind") or "") + (x.get("daypower") or "")}
                      for x in (c.get("casts") or [])]}
