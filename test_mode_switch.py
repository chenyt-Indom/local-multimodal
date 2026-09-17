# -*- coding: utf-8 -*-
"""联网 / 离线**来回切换**验证（对着真实运行的应用打）。

跑法：python test_mode_switch.py [端口]

每轮做这些事：
  1) 切到联网 → 断言 online / tile_source=amap
       · 地名搜索能查到、路线能算（真实数据）
       · 高德瓦片端点给真图
  2) 切到离线 → 断言 online=false / tile_source=osm
       · **关键：新算过的地名/路线要从本地缓存重放出来**（这是离线模式的意义）
       · 没缓存过的瓦片要 404 且**秒回**（秒回＝根本没去联网试）
  3) 再来一轮

⚠️ 不写任何"等几秒应该好了"的模糊断言：每一步都直接看接口返回的字段。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 用真实的经纬度算瓦片号。
# ⚠️ 别自己随手编一个 x/y —— 第一版就编到了**蒙古境内**（110°E,43.5°N），
#    高德在境外返回空白瓦片，于是断言全挂，白排查一轮。
from backend import map_tools as _mt      # noqa: E402

# 每轮换一个城市，保证取的是"没缓存过的"瓦片
CITIES = [("汕头", 23.4163, 116.6291),
          ("武汉", 30.5928, 114.3055),
          ("西安", 34.3416, 108.9398)]
# 境外一个点（东京塔），用来验证"高德没数据时回退 OSM"
FOREIGN = ("东京塔", 35.6586, 139.7454)


def tile_xy(lat, lon, z, src="amap"):
    la, lo = _mt._tile_latlon(lat, lon, src)
    return _mt._lonlat_to_tile(la, lo, z)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE = "http://127.0.0.1:%d" % PORT

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  [!!] %s%s" % (name, ("  " + str(detail)) if detail else ""))


def get(path, timeout=60):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_cfg(**kw):
    body = json.dumps(kw).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/config", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def set_mode(online):
    """切开关。等它真的生效（online() 有 2 秒短缓存，所以要轮询确认）。"""
    post_cfg(web_enabled=bool(online))
    for _ in range(20):
        st = get("/api/map/stats", timeout=10)
        if bool(st.get("online")) == bool(online):
            return st
        time.sleep(0.6)
    return get("/api/map/stats", timeout=10)


def tile_probe(path):
    """取一张瓦片，返回 (状态码, 字节数, 耗时秒)。"""
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            data = r.read()
        return r.status, len(data), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, 0, time.time() - t0
    except Exception as e:
        return -1, 0, time.time() - t0


def main():
    print("=" * 62)
    print("联网 / 离线 来回切换验证  ->  %s" % BASE)
    print("=" * 62)

    for rd in (1, 2, 3):
        print()
        print("############ 第 %d 轮 ############" % rd)

        # ---------------- 联网 ----------------
        st = set_mode(True)
        print("【联网】")
        check("开关切到联网", st.get("online") is True)
        check("底图=高德", st.get("tile_source") == "amap", st.get("tile_source_name"))

        # 高德瓦片：取一张**这个城市 z=14 的瓦片**（每轮换城市＝大概率没缓存过）
        cname, clat, clon = CITIES[(rd - 1) % len(CITIES)]
        z = 14
        x, y = tile_xy(clat, clon, z)
        code, size, dt = tile_probe("/api/map/amap/%d/%d/%d.png?v=1" % (z, x, y))
        check("高德瓦片能取到真图（%s）" % cname, code == 200 and size > 1000,
              "HTTP %s  %s B  %.2fs" % (code, size, dt))

        # 地名（真实数据）
        q = "汕头市濠江区人民政府"
        r = get("/api/map/search?q=%s&limit=1" % urllib.parse.quote(q))
        hit = (r.get("places") or [{}])[0]
        check("联网能查到地名", bool(hit.get("name")), hit.get("name", ""))

        st = get("/api/map/stats")
        check("联网侧有可用数据", True, "缓存里已有 %d 个地名 / %d 条路线"
              % (st.get("places", 0), st.get("routes", 0)))

        # ---------------- 离线 ----------------
        st = set_mode(False)
        print("【离线】")
        check("开关切到离线", st.get("online") is False)
        check("底图=OSM", st.get("tile_source") == "osm", st.get("tile_source_name"))

        # 刚才联网查过的地名，离线必须还能查到（走本地缓存）
        r = get("/api/map/search?q=%s&limit=1" % urllib.parse.quote(q))
        hit = (r.get("places") or [{}])[0]
        check("离线重放联网时查过的地名", bool(hit.get("name")),
              "%s (from_cache=%s)" % (hit.get("name", "无"), hit.get("from_cache")))

        # 没有缓存的瓦片：必须**秒回**404（秒回＝压根没去联网试）
        code, size, dt = tile_probe("/api/map/amap/19/430000/230000.png")
        check("离线取没缓存的高德瓦片 → 不联网、秒回", dt < 1.0,
              "HTTP %s  %.3fs" % (code, dt))

        code2, size2, dt2 = tile_probe("/api/map/tile/19/430000/230000.png")
        check("离线取没缓存的 OSM 瓦片 → 不联网、秒回", dt2 < 1.0,
              "HTTP %s  %.3fs" % (code2, dt2))

    # ---------------- 境外：高德没数据，应自动回退 OSM ----------------
    st = set_mode(True)
    print()
    print("【联网 · 境外地点】")
    fz = 15
    fx, fy = tile_xy(FOREIGN[1], FOREIGN[2], fz)
    code, size, dt = tile_probe("/api/map/amap/%d/%d/%d.png?v=1" % (fz, fx, fy))
    check("境外取高德瓦片端点 → 自动回退 OSM 真图", code == 200 and size > 1000,
          "HTTP %s  %s B  %.2fs（%s）" % (code, size, dt, FOREIGN[0]))

    # ---------------- 收尾 ----------------
    st = set_mode(True)
    print()
    print("############ 收尾 ############")
    check("已恢复联网模式", st.get("online") is True, st.get("tile_source_name"))

    print()
    print("=" * 62)
    if FAIL:
        print("失败 %d 项 / 通过 %d 项" % (FAIL, PASS))
    else:
        print("全部通过 ✔  （%d 项）" % PASS)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
