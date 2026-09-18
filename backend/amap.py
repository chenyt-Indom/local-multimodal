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
import time
import urllib.parse
import urllib.request

AMAP = "https://restapi.amap.com/v3"
AMAP_V4 = "https://restapi.amap.com/v4"      # 骑行在这套（v3 没有骑行接口）
_UA = "local-multimodal-assistant/1.0"


# ---------------------------------------------------------------- key
def stored_key() -> str:
    """配置里**存着**的 key（不管有没有断开）。

    和 `key()` 的区别很重要：断开只是"不用它"，key 本身要留着 ——
    用户明确要求"断开后还要重新输一遍太麻烦"。
    """
    try:
        from . import config
        return str(config.load_config().get("amap_key") or "").strip()
    except Exception:
        return ""


def enabled() -> bool:
    """高德总开关（界面上的「连接 / 断开」）。断开时 key 仍在，只是不生效。"""
    try:
        from . import config
        return bool(config.load_config().get("amap_enabled", True))
    except Exception:
        return True


def key() -> str:
    """**当前实际可用**的 key：配了 + 没断开才返回，否则空串。

    调用方据此自动回退到 OpenStreetMap —— 所以"断开"只要让这里返回空，
    全链路（找地点 / 路线 / 周边 / 底图）就一起切回 OSM 了。
    """
    if not enabled():
        return ""
    return stored_key()


def has_key() -> bool:
    return bool(key())


def has_stored_key() -> bool:
    """配了 key 就算（哪怕现在是断开状态）—— 用来判断"要不要请用户填 key"。"""
    return bool(stored_key())


def has_key() -> bool:
    return bool(key())


def verify_key(k: str) -> tuple:
    """拿一个 key 真去调一次高德，确认能用。返回 (是否可用, 给用户看的话)。

    ⚠️ 一定要**真调一次**，不能只看格式：用户复制时经常多带空格，
    或者把「Web端(JS API)」的 key 拿来用 —— 那个平台在服务端接口上是无效的。
    不验证的话他会以为配好了，实际地图一直静静退回 OpenStreetMap，
    然后来问"为什么不是高德"，很难查。这里宁可当场报错。

    用**地理编码**做验证：它最便宜、最稳定，而且返回结果能顺便让用户确认
    "真的通了"（我们直接回一个真实地名给他看）。
    """
    k = str(k or "").strip()
    if not k:
        return False, "没有拿到 key。"
    if len(k) != 32:
        return False, ("这个 key 长度是 %d 位，高德的 key 是 **32 位**。"
                       "可能复制的时候少了一段或者多了空格。" % len(k))
    url = (AMAP + "/geocode/geo?" + urllib.parse.urlencode(
        {"key": k, "address": "汕头大学", "output": "json"}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, "连不上高德服务器（%s）。检查一下网络再试。" % str(e)[:60]
    if str(d.get("status")) == "1":
        geo = (d.get("geocodes") or [{}])[0]
        name = geo.get("formatted_address") or geo.get("province") or "某个地点"
        return True, ("✅ 高德已连接（用这个 key 查到了一个真实地点：%s）。" % name)
    info = str(d.get("info") or "").strip()
    code = str(d.get("infocode") or "").strip()
    if "INVALID_USER_KEY" in info or code == "10001":
        return False, ("这个 key 高德不认（INVALID_USER_KEY）。常见的两个原因：\n"
                       "① 复制的时候少了几位、或者带了空格；\n"
                       "② 建 Key 时**服务平台选错了** —— 必须选「**Web服务**」，"
                       "选成「Web端(JS API)」或「iOS/Android」的 key 在服务端用不了。\n"
                       "去 https://console.amap.com/dev/key/app 重新建一个即可。")
    if "DAILY_QUERY_OVER_LIMIT" in info or code == "10003":
        return True, ("✅ key 是有效的，但今天的免费额度已经用完了（%s）。"
                      "明天会自动恢复。" % info)
    if "SERVICE_NOT_AVAILABLE" in info or code == "10009":
        return False, ("这个 key 没开通「Web服务」这个服务（%s）。"
                       "去控制台给这个 key 勾上「Web服务」再试。" % info)
    return False, "高德返回了错误：%s（%s）。检查一下 key 是否正确。" % (info or "未知", code)



# ---------------------------------------------------------------- 健康状态
# "高德 key 还能不能用"这个结论要**缓存**：它每次都得真调一次网络，
# 不能在前端轮询、系统提示拼装这些高频路径上直接调。
# 正常时 15 分钟复查一次；一旦异常改成 1 分钟一次 —— 恢复了要尽快发现，
# 别让用户对着一个"已失效"的红标继续用。
_HEALTH_TTL_OK = 15 * 60
_HEALTH_TTL_BAD = 60
_HEALTH = {"ok": None, "message": "", "checked": 0.0, "key_hint": ""}


def _key_hint(k: str) -> str:
    return (k[:6] + "…" + k[-4:]) if len(k) >= 12 else ("已配置" if k else "")


def check_health(force: bool = False, online: bool = True) -> dict:
    """高德当前处于什么状态。返回一个**给前端和模型看**的字典。

    四种状态（前端按钮就按这个分档）：
      configured=False              → 没配过 key         → 按钮暗
      configured=True, enabled=False → 用户主动**断开**   → 按钮暗（key 还在！）
      enabled=True, ok=False         → 配了但**不可用**   → 按钮暗 + 红标
      enabled=True, ok=True          → 正常              → 按钮**亮起**

    · message：给人看的一句话（断开/不可用都说清原因）
    · checked_at / age_s：上次检测的时间与距今多少秒
    """
    st = stored_key()
    now = time.time()
    if not st:
        _HEALTH.update(ok=False, message="还没配高德 key", checked=now, key_hint="")
        return _health_dict(configured=False)
    if not enabled():
        # ⚠️ 断开是**用户主动的选择**，不是故障：不检测、也不报红，
        #    而且要明确告诉他们"key 还留着"（不然会以为又要重输一遍）。
        _HEALTH.update(ok=False, message="已断开（key 还留着，点「连接」就能恢复）",
                       checked=now, key_hint=_key_hint(st))
        return _health_dict(configured=True)
    if not online:
        # 离线模式不联网：保留上次结论，但不刷新（避免把"没网"误判成"key 坏了"）
        return _health_dict(configured=True, skipped="离线模式，暂不检测")
    hint = _key_hint(st)
    same = (_HEALTH["key_hint"] == hint)
    ttl = _HEALTH_TTL_OK if _HEALTH["ok"] else _HEALTH_TTL_BAD
    if force or (not same) or (now - float(_HEALTH["checked"] or 0) > ttl):
        ok, msg = verify_key(st)
        _HEALTH.update(ok=bool(ok), message=msg, checked=time.time(), key_hint=hint)
    return _health_dict(configured=True)


def _health_dict(configured: bool, skipped: str = "") -> dict:
    age = (time.time() - float(_HEALTH["checked"] or 0)) if _HEALTH["checked"] else None
    return {"configured": bool(configured),
            "enabled": bool(enabled()),
            "ok": bool(_HEALTH["ok"]),
            "message": _HEALTH["message"] or "",
            "key_hint": _HEALTH["key_hint"] or "",
            "checked_at": _HEALTH["checked"] or 0.0,
            "age_s": round(age, 1) if age is not None else None,
            "skipped": skipped}


def invalidate_health() -> None:
    """key 被改过（填了新的 / 清空了）→ 丢掉旧结论，下次一定重新检测。"""
    _HEALTH.update(ok=None, message="", checked=0.0, key_hint="")


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
    """关键词搜 POI（必须带 city，否则高德一律返回 0 条）。

    ⚠️ 排序**按名字匹配度**，不按评分 —— 这个函数是拿来"找某个地方"的，
       不是"挑评分高的"。实测按评分排会出岔子：查「广州天河城」会把
       「番禺天河城」（评分更高）排在「天河城」前面。
       高德自己返回的顺序本来就是按相关度，保持不动，只把**名字完全一致**的提前。
    """
    d = _get("/place/text", keywords=keywords, city=city, page=page,
             offset=min(int(offset or 20), 25), extensions="all")
    if not d:
        return {"ok": False, "items": []}
    items = [x for x in (_poi(p) for p in (d.get("pois") or [])) if x]
    kw = str(keywords or "").strip()
    if kw:
        items.sort(key=lambda x: 0 if (x.get("name") or "").strip() == kw else 1)
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
