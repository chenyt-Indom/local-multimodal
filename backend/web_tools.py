# -*- coding: utf-8 -*-
"""联网搜索 + 调用外部 API 能力。

- 该功能由前端开关控制（默认关闭）。仅在用户打开“联网搜索”时才会发起公网请求。
- 搜索：使用 Bing 网页搜索（无需 API Key）+ 抓取前 N 条结果摘要。
- 调用外部 API：用户可配置 API 端点，AI 通过"工具模式"调用（例如查天气、查时间等）。
完全符合"按钮控制（开才联网）"的约定：默认本地、数据不出机，开开关才联网。
"""
import json
import urllib.parse
import urllib.request

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=WEB_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _clean(s: str) -> str:
    """去标签 + 反转义 + 压缩空白。"""
    import html as _html
    import re
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _parse_h2_blocks(html: str, n: int, engine: str) -> list:
    """通用解析：<h2 ...><a href=URL>TITLE</a></h2> … <p …>DESC</p>（Bing 系）。"""
    import re
    out = []
    pattern = re.compile(
        r"<h2[^>]*>\s*<a[^>]*href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>\s*</h2>(.*?)(?=<h2|\Z)",
        re.S,
    )
    for m in pattern.finditer(html):
        if len(out) >= n:
            break
        title = _clean(m.group(2))
        if not title:
            continue
        dm = re.search(r"<p[^>]*>(.*?)</p>", m.group(3), re.S)
        desc = _clean(dm.group(1)) if dm else ""
        out.append({"title": title, "url": m.group(1).strip(),
                    "desc": desc[:300], "engine": engine})
    return out


def _search_bing(query: str, n: int) -> list:
    """Bing 中文站。"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + f"&count={n}"
    return _parse_h2_blocks(_fetch(url), n, "bing")


def _search_bing_en(query: str, n: int) -> list:
    """Bing 国际版（结果池与中文站不同，可互补）。"""
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query)
           + f"&count={n}&setlang=en")
    return _parse_h2_blocks(_fetch(url), n, "bing-intl")


def _search_360(query: str, n: int) -> list:
    """360 搜索。

    实测要点（此前解析方式完全错了）：
    - 有机结果在 `<li class="res-list">` 内，不是裸的 `<h3 class="g-title">`。
      整页 g-title 有 120+ 个，绝大多数来自侧栏组件/相关搜索，
      而真正的结果只有 9 个左右 —— 按 g-title 抓会把大量垃圾当成结果。
    - 结果链接带 `data-mdurl`，那是**真实目标网址**（官网原址）；
      而 `href` 是 `so.com/link?m=...` 跳转包装。
      此前一直拿跳转链接当 URL，导致「按域名判断权威性」完全失效
      （所有结果的域名都是 so.com）。
    - 摘要在 `<p class="res-desc">`，此前被写死成空字符串，
      等于 1/3 的引擎只贡献标题、不贡献任何正文线索。
    """
    import re
    html = _fetch("https://www.so.com/s?q=" + urllib.parse.quote(query))
    out = []
    for block in re.split(r'<li[^>]*class="[^"]*res-list', html)[1:]:
        if len(out) >= n:
            break
        block = block[:6000]
        tm = re.search(r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>',
                       block, re.S)
        if not tm:
            continue
        title = _clean(tm.group(1))
        if len(title) < 3:
            continue
        anchor = block[:block.find("</a>") + 4] if "</a>" in block else block[:600]
        um = (re.search(r'data-mdurl="([^"]+)"', anchor)
              or re.search(r'href="([^"]+)"', anchor))
        url = um.group(1).strip() if um else ""
        if url.startswith("//"):
            url = "https:" + url
        dm = re.search(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)(?:</p>|<p\b)',
                       block, re.S)
        desc = _clean(dm.group(1)) if dm else ""
        out.append({"title": title, "url": url, "desc": desc, "engine": "360"})
    return out


def _search_sogou(query: str, n: int) -> list:
    """搜狗搜索（**已失效，保留仅为记录**）。

    2026-09 实测：搜狗已启用反爬，`sogou.com/web?query=` 返回的是
    5KB 的「验证码 / antispider」拦截页，任何结构都解析不出结果。
    保留此函数是为了避免后来者重复踩坑——不要再把它加回 _ENGINES。
    """
    import re
    html = _fetch("https://www.sogou.com/web?query=" + urllib.parse.quote(query))
    out = []
    # 依次尝试几种常见的标题容器结构
    patterns = [
        r"<h3[^>]*class=\"[^\"]*vr-title[^\"]*\"[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
        r"<h3[^>]*>\s*<a[^>]*href=\"(/link\?url=[^\"]+|https?://[^\"]+)\"[^>]*>(.*?)</a>",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.S):
            if len(out) >= n:
                break
            title = _clean(m.group(2))
            if len(title) < 3:
                continue
            url = m.group(1).strip()
            if url.startswith("/"):
                url = "https://www.sogou.com" + url
            out.append({"title": title, "url": url, "desc": "", "engine": "sogou"})
        if out:
            break
    return out


# 搜索引擎自家的"频道页/二次搜索页"：标题往往等于查询词，相关度虚高，
# 点进去还是搜索框，对用户毫无价值。实测 360 会返回一堆这类结果
# （"XXX - 360文库""XXX (约154个相关视频) - 360视频"），必须剔除。
_SELF_CHANNEL_JUNK = (
    "ai.so.com/search", "ai.so.com/?", "news.so.com/ns", "wenku.so.com/s",
    "image.so.com/i", "video.so.com", "tv.so.com", "map.so.com",
    "so.com/link?", "so.com/s?", "so.com/search",
    "?src=ob_zz", "&src=imageonebox", "src=imageonebox",
    "baidu.com/s?", "zhihu.com/search?", "sogou.com/web?",
)
# 明显不是目标内容的标题特征
_SELF_CHANNEL_TITLE = (
    "360视频", "360文库", "360图片", "360百科导航", "相关视频)", "相关搜索",
    "高清在线观看", "百度图片", "图片大全", "视频大全",
)


def _is_useless(item: dict) -> bool:
    """判断一条结果是否属于"搜索引擎自家的频道页"这类噪音。"""
    url = (item.get("url") or "").strip()
    if not url.startswith("http"):
        return True
    low = url.lower()
    if any(t in low for t in _SELF_CHANNEL_JUNK):
        return True
    title = (item.get("title") or "")
    if any(t in title for t in _SELF_CHANNEL_TITLE):
        return True
    return False


def _merge_engines(batches: list, n: int, query: str = "") -> list:
    """轮询合并各引擎结果并去重，保证来源多样。

    顺带标记 `_engines`（同一条结果被几个引擎收录）——多方一致说明更可信。

    排序综合两项：**相关度（权重 3）+ 来源质量**。
    只按相关度排会让百科/聚合站霸榜；只按质量排又会把权威但跑题的页面顶上来，
    所以两者都要看。
    """
    import re
    seen, order, idx = {}, [], 0
    while True:
        added = False
        for batch in batches:
            if idx < len(batch):
                item = batch[idx]
                if _is_useless(item):
                    added = True          # 计入"本轮有推进"，继续往下扫
                    continue
                key = re.sub(r"\W+", "", item.get("title", ""))[:18]
                if key:
                    if key in seen:
                        rec = seen[key]
                        rec["_engines"] = min(rec.get("_engines", 1) + 1, len(batches))
                        # 后出现的引擎若摘要更完整，用它补上
                        if not (rec.get("desc") or "").strip():
                            rec["desc"] = item.get("desc") or ""
                        added = True
                        continue
                    rec = dict(item)
                    rec["_engines"] = 1
                    seen[key] = rec
                    order.append(rec)
                    added = True
        if not added:
            break
        idx += 1

    def _score(it):
        rel = relevance_ratio(query, it) if query else 0.0
        return -(3.0 * rel + _quality_score(it, it.get("_engines", 1)))

    return sorted(order, key=_score)[:n]


# 可用的检索引擎（并行调用，任一成功即可返回结果）
#
# 取舍记录（都实测过，别凭感觉改）：
# - **搜狗**：已上反爬，返回的是「验证码/antispider」页（5KB），
#   解析结果为 0 条。留着只会白占一个并发位、拉长整体等待，故移除。
# - **国际版 Bing**：中文查询几乎全军覆没（实测相关度全部 0.00），不纳入。
# - **DuckDuckGo / Brave / Mojeek / Ecosia**：国内不可达（超时或 403）。
# - **百度**：返回 1.4KB 的跳转壳页，需 JS 执行，无法直接解析。
# - **360**：支持 `site:` 定向语法（Bing 中文站会忽略 site:），
#   是查机构官方信息的唯一有效通道，详见 search_official()。
_ENGINES = (_search_bing, _search_360)


# 机构/官方信息类查询 → 建议定向的域名后缀
_ORG_HINTS = (
    (("大学", "学院", "学校", "中学", "小学", "职业技术学院", "高校",
      "招生", "录取", "研究生院"), "edu.cn"),
    (("政府", "人民政府", "管理局", "委员会", "发改委", "教育局", "公安局",
      "卫健委", "统计局", "财政局", "人社局", "住建局", "税务局", "气象局",
      "应急管理", "市场监督", "厅", "部委", "街道办", "政务"), "gov.cn"),
    (("协会", "学会", "基金会", "研究院", "研究所", "工会", "联合会"), "org.cn"),
)


def official_site_hint(query: str) -> str:
    """判断查询是否属于"查某机构官方信息"，返回建议的 site: 后缀。"""
    if not query:
        return ""
    for words, tld in _ORG_HINTS:
        if any(w in query for w in words):
            return tld
    return ""


def search_official(query: str, n: int = 8) -> list:
    """定向检索权威站点（`关键词 site:gov.cn` 之类）。

    为什么需要：查"某单位对外公开情况"时，普通检索会被百科、聚合站、
    甚至同名地名的词条淹没。加了 site: 限定后，返回的基本都是官网本身。
    实测 `深圳大学 招生章程 site:edu.cn` → 首条即 `zs.szu.edu.cn`（官方招生网）；
    `国家统计局 统计公报 site:gov.cn` → 全部是 `stats.gov.cn` 官方页面。

    只有 360 支持该语法，因此这里不并发多引擎。
    """
    hint = official_site_hint(query)
    if not hint:
        return []
    try:
        return _search_360(f"{query} site:{hint}", n)
    except Exception:
        return []


# 信息量极低的字：单独成 gram 时无区分度
_LOW_INFO_CHARS = set("的了吗呢吧啊呀哦嗯是我你他她它们这那有和与及或在为对从到把被让给就都还也"
                      "很更最不没要会能可以着过之其所以于而且则使被当把从")

# 常见的提问填充词：不参与相关性打分
_FILLER_PHRASES = (
    "请问", "帮我", "帮忙", "查询", "查一下", "查查", "一下", "怎么样", "怎样",
    "如何", "多少", "是什么", "什么", "哪些", "哪个", "介绍", "告诉我", "想知道",
    "了解一下", "了解", "最新", "近期", "最近", "现在", "目前", "今年", "有没有",
    "关于", "以及", "还有", "情况", "信息", "内容", "相关", "麻烦", "谢谢",
)

# 相对权威的域名（命中即加权）——机构公开信息优先看官方口径
_AUTHORITY_TLD = (".gov.cn", ".edu.cn", ".org.cn", ".ac.cn",
                  ".gov", ".edu", ".mil")
_AUTHORITY_SITES = (
    "gov.cn", "edu.cn", "org.cn", "ac.cn",
    "xinhuanet.com", "people.com.cn", "chinanews.com", "cctv.com", "cnr.cn",
    "thepaper.cn", "caixin.com", "yicai.com", "21jingji.com", "stcn.com",
    "cnstock.com", "cs.com.cn", "eastmoney.com", "sse.com.cn", "szse.cn",
    "stats.gov.cn", "moe.gov.cn", "miit.gov.cn", "mof.gov.cn",
    "cma.gov.cn", "weather.com.cn", "nmpa.gov.cn", "samr.gov.cn",
)
# 可信度偏低：UGC / 文库 / 营销号
_LOW_TRUST_SITES = (
    "wenku.baidu.com", "zhidao.baidu.com", "tieba.baidu.com", "docin.com",
    "360doc.com", "doc88.com", "baijiahao.baidu.com", "toutiao.com",
    "xiaohongshu.com", "douban.com",
)


def _terms(query: str) -> dict:
    """抽取用于相关性打分的词元 → {词: 权重}。

    **为什么不用 re.split 切词**：中文查询通常没有空格，
    按空白切会把「明天上海天气预报」变成一整个串，既匹配不上任何结果，
    也丢掉了「上海/天气」才是关键信息这件事。实测后果是
    搜「明天上海天气预报」把鲁迅的短篇小说《明天》当成结果返回。

    这里用 2~4 字 n-gram 近似分词，权重 = 长度²：
    越长越具体，「天气预报」（16）自然远重过「明天」（4）。
    """
    import re
    if not query:
        return {}
    q = query.lower()
    terms: dict = {}
    for seg in re.split(r"[\s,，、/;；:：()（）\[\]【】\"'!！?？.。\-—_|]+", q):
        if not seg:
            continue
        # 纯英文/数字：整段作为一个词
        if re.fullmatch(r"[a-z0-9]+", seg):
            if len(seg) >= 2:
                terms[seg] = max(terms.get(seg, 0), len(seg) ** 2)
            continue
        n = len(seg)
        for size in (4, 3, 2):
            if size > n:
                continue
            for i in range(n - size + 1):
                g = seg[i:i + size]
                if not re.search(r"[0-9a-z\u4e00-\u9fff]", g):
                    continue
                if g in _FILLER_PHRASES:
                    continue
                if all((c in _LOW_INFO_CHARS) for c in g):
                    continue
                terms[g] = max(terms.get(g, 0), size ** 2)
    return terms


def relevance_ratio(query: str, item: dict) -> float:
    """一条结果对查询的相关度（0~1）：命中词元权重 / 总权重。"""
    terms = _terms(query)
    if not terms:
        return 1.0
    total = sum(terms.values()) or 1
    text = ((item.get("title") or "") + " " + (item.get("desc") or "")).lower()
    if not text.strip():
        return 0.0
    hit = sum(w for g, w in terms.items() if g in text)
    return hit / total


def best_relevance(query: str, items: list) -> float:
    """一批结果里最高的相关度——供上层判断"这次搜索靠不靠谱"。"""
    if not items:
        return 0.0
    return max(relevance_ratio(query, it) for it in items)


def filter_relevant(query: str, items: list, keep_min: int = 3) -> list:
    """按相关度过滤并排序，剔除答非所问的结果。

    规则：至少达到「最佳结果的 40%」且不低于 15% 的绝对命中。
    若这样筛完不足 keep_min 条，则按分数补齐前 keep_min 条 ——
    **保证不返回空**，避免上层退回"未过滤的原始结果"（那等于把垃圾又放回来）。
    """
    if not items:
        return []
    scored = sorted(((relevance_ratio(query, it), it) for it in items),
                    key=lambda x: -x[0])
    best = scored[0][0]
    cutoff = max(best * 0.4, 0.15)
    kept = [it for r, it in scored if r >= cutoff]
    if len(kept) < keep_min:
        kept = [it for _, it in scored[:keep_min]]
    return kept


def _quality_score(item: dict, engine_count: int) -> float:
    """来源可信度打分：官方站优先，空摘要降权。

    「某单位对外公开情况」这类查询，用户要的是官方口径，
    而引擎往往把百科/聚合站排在前面，所以这里显式拔高 .gov.cn/.edu.cn。

    engine_count = 这条结果被几个引擎同时收录（≥2 说明多方共识）。
    """
    url = (item.get("url") or "").lower()
    desc = (item.get("desc") or "").strip()
    q = 0.0
    for d in _LOW_TRUST_SITES:
        if d in url:
            q -= 1.5
            break
    for s in _AUTHORITY_SITES:
        if s in url:
            q += 3.0
            break
    else:
        # 泛化的 TLD 判定（避免逐个枚举所有政府站）
        if any(t in url for t in _AUTHORITY_TLD):
            q += 2.5
    if not desc:
        q -= 1.0          # 只有标题没法据以作答
    elif len(desc) >= 60:
        q += 0.5          # 摘要有实质内容
    if engine_count >= 2:
        q += 1.0          # 多引擎共识
    return q



def web_search(query: str, n: int = 8) -> list:
    """多引擎并行检索，合并去重后返回 [{title, url, desc, engine}]。

    相比单引擎：覆盖面更广，某个引擎抽风或没有结果时其它引擎可兜底。
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(_ENGINES)) as pool:
        futures = [pool.submit(fn, query, n) for fn in _ENGINES]
        batches = []
        for f in futures:
            try:
                batches.append(f.result(timeout=20) or [])
            except Exception:
                batches.append([])

    merged = _merge_engines(batches, n, query=query)
    if merged:
        return merged

    # 全部失败时的兜底：给出可点击的搜索地址
    q = urllib.parse.quote(query)
    return [{"title": "（未获取到搜索结果，可点开下列链接自行查看）",
             "url": f"https://cn.bing.com/search?q={q}",
             "desc": f"备选：https://www.so.com/s?q={q}",
             "engine": "fallback"}]


# ---------- 天气查询（结构化数据，不用搜索引擎）----------
# 为什么单独做：搜索引擎对"明天上海天气"只会返回「上海天气预报_15天」这类
# 导航页，拿不到任何真实数值。天气必须走数据 API。
# 选 Open-Meteo：免密钥、免注册、支持中文城市名、有全球数据。

_WEATHER_CODE = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}


def _json_get(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def geocode_city(city: str, count: int = 5) -> list:
    """城市名 → 经纬度（Open-Meteo 地理编码，支持中文）。"""
    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + urllib.parse.quote(city)
           + f"&count={count}&language=zh&format=json")
    try:
        data = _json_get(url)
    except Exception:
        return []
    out = []
    for r in data.get("results", []) or []:
        try:
            out.append({
                "name": r.get("name") or "",
                "admin1": r.get("admin1") or "",
                "admin2": r.get("admin2") or "",
                "country": r.get("country") or "",
                "lat": float(r["latitude"]),
                "lon": float(r["longitude"]),
            })
        except Exception:
            continue
    return out


def weather(city: str, days: int = 3) -> dict:
    """查某地天气，返回结构化数据。

    返回 {ok, city, admin, lat, lon, current:{...}, daily:[{date,high,low,rain,desc}], note}
    """
    days = max(1, min(int(days or 3), 16))
    hits = geocode_city(city)
    if not hits:
        return {"ok": False, "error": f"没找到城市「{city}」。可以换个说法，比如「上海市」「广东 深圳」"}

    # 优先取在国家/省份层级匹配得上的那个（避免"上海"命中云南的小地名）
    best = hits[0]
    for h in hits:
        if h["name"] == city or city in (h["name"] + h["admin1"]):
            best = h
            break

    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
           "precipitation,weather_code,wind_speed_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_sum,precipitation_probability_max,wind_speed_10m_max"
           "&timezone=Asia%%2FShanghai&forecast_days=%d" % (best["lat"], best["lon"], days))
    try:
        d = _json_get(url, timeout=25)
    except Exception as e:
        return {"ok": False, "error": f"天气服务请求失败：{e}"}

    cur_raw = d.get("current") or {}
    daily_raw = d.get("daily") or {}
    dates = daily_raw.get("time") or []

    daily = []
    for i, day in enumerate(dates):
        code = (daily_raw.get("weather_code") or [None] * len(dates))[i]
        daily.append({
            "date": day,
            "high": (daily_raw.get("temperature_2m_max") or [None] * len(dates))[i],
            "low": (daily_raw.get("temperature_2m_min") or [None] * len(dates))[i],
            "rain_mm": (daily_raw.get("precipitation_sum") or [None] * len(dates))[i],
            "rain_pct": (daily_raw.get("precipitation_probability_max") or [None] * len(dates))[i],
            "wind": (daily_raw.get("wind_speed_10m_max") or [None] * len(dates))[i],
            "desc": _WEATHER_CODE.get(code, f"天气码{code}" if code is not None else ""),
        })

    cur = {}
    if cur_raw:
        code = cur_raw.get("weather_code")
        cur = {
            "temp": cur_raw.get("temperature_2m"),
            "feels": cur_raw.get("apparent_temperature"),
            "humidity": cur_raw.get("relative_humidity_2m"),
            "rain_mm": cur_raw.get("precipitation"),
            "wind": cur_raw.get("wind_speed_10m"),
            "desc": _WEATHER_CODE.get(code, f"天气码{code}" if code is not None else ""),
        }

    # 行政区划去重："上海市 上海市" 这种重复只留一个
    parts = [p for p in (best.get("admin1"), best.get("admin2")) if p]
    admin = " ".join(dict.fromkeys(parts))

    return {
        "ok": True,
        "city": best["name"],
        "admin": admin,
        "country": best["country"],
        "lat": best["lat"], "lon": best["lon"],
        "current": cur,
        "daily": daily,
        "source": "Open-Meteo（open-meteo.com）",
        "note": "数据为气象模型预报值，与中央气象台发布可能略有差异。",
    }


def format_weather(w: dict) -> str:
    """把天气数据格式化成给模型看的紧凑文本。"""
    if not w.get("ok"):
        return f"天气查询失败：{w.get('error')}"
    lines = [f"【{w['city']}{('（' + w['admin'] + '）') if w.get('admin') else ''} 天气】"
             f"坐标 {w['lat']:.2f},{w['lon']:.2f}　数据源：{w.get('source','')}"]
    c = w.get("current") or {}
    if c:
        lines.append(f"当前：{c.get('desc','')}，气温 {c.get('temp')}°C"
                     f"（体感 {c.get('feels')}°C），湿度 {c.get('humidity')}%，"
                     f"降水 {c.get('rain_mm')}mm，风速 {c.get('wind')}km/h")
    lines.append("逐日预报：")
    for i, d in enumerate(w.get("daily") or []):
        tag = ["今天", "明天", "后天"][i] if i < 3 else f"第{i+1}天"
        lines.append(f"  {d['date']}（{tag}）{d['desc']}　"
                     f"{d['low']}~{d['high']}°C　降水概率 {d['rain_pct']}%"
                     f"（{d['rain_mm']}mm）　最大风速 {d['wind']}km/h")
    if w.get("note"):
        lines.append(f"说明：{w['note']}　回答时请注明数据来源。")
    return "\n".join(lines)


# ---------- 图片搜索（找现成的图，不是生成图）----------
IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://cn.bing.com/",       # 绕开多数站点的防盗链
}


def _clean_img_query(q: str) -> str:
    """去掉「照片/图片/壁纸」等冗余词。

    实测：Bing 图片搜索对这类后缀很敏感——搜「埃菲尔铁塔」结果准确，
    搜「埃菲尔铁塔 照片」却返回完全无关的内容。因此搜索前先剥离这些词。
    """
    import re
    s = re.sub(r"(照片|图片|图像|壁纸|高清|大图|素材|头像|png|jpg|jpeg)",
               " ", q or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def image_search(query: str, n: int = 4) -> list:
    """联网搜图：返回 [{title, url, thumb, source}]。

    **与 generate_image 的区别**：这里找的是网上已存在的图片（原图直出），
    不做任何绘制；用户要求「找张图/搜张图/给我看看 xx 长什么样」时用它。
    """
    import html as _html
    import re

    def _run(q: str) -> list:
        url = ("https://cn.bing.com/images/search?q=" + urllib.parse.quote(q)
               + f"&count={max(n * 4, 24)}")
        try:
            page = _fetch(url)
        except Exception:
            return []
        out = []
        for m in re.finditer(r'class="iusc"[^>]*m="([^"]+)"', page):
            try:
                data = json.loads(_html.unescape(m.group(1)))
            except Exception:
                continue
            murl = (data.get("murl") or "").strip()
            if not murl.startswith("http"):
                continue
            out.append({
                "title": _clean(data.get("t") or ""),
                "url": murl,
                "thumb": (data.get("turl") or "").strip(),
                "source": (data.get("purl") or "").strip(),
            })
        return out

    # 先用清洗后的关键词（更准），数量不足再用原始词补充
    cleaned = _clean_img_query(query)
    results = []
    if cleaned and cleaned != query:
        results = _run(cleaned)
    if len(results) < n:
        results += _run(query)

    # 去重并按原图 URL 收敛
    seen, final = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        final.append(r)
        if len(final) >= n:
            break
    return final


def download_image(url: str, max_bytes: int = 8 * 1024 * 1024,
                   timeout: int = 25) -> bytes | None:
    """下载图片原始字节（限制体积，避免大图拖垮前端）。"""
    try:
        req = urllib.request.Request(url, headers=IMG_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return None
            raw = resp.read(max_bytes + 1)
        return None if len(raw) > max_bytes else raw
    except Exception:
        return None


# ---------- 调用外部 API ----------
def call_external_api(config: dict, tool: str, params: dict) -> dict:
    """根据已配置的 API 工具执行调用。

    config 形如:
      {
        "tools": [
          {"name": "天气查询", "method": "GET", "url": "https://api.example.com/weather?city={city}"},
        ]
      }
    匹配 tool 名称后替换占位符并请求。
    """
    tools = config.get("tools", [])
    for t in tools:
        if t.get("name") == tool:
            url = t.get("url", "")
            for k, v in params.items():
                url = url.replace("{" + k + "}", urllib.parse.quote(str(v)))
            method = t.get("method", "GET").upper()
            headers = dict(WEB_HEADERS)
            if t.get("api_key"):
                headers["Authorization"] = "Bearer " + t["api_key"]
            req = urllib.request.Request(url, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
                # 尝试 JSON 解析，失败则返回原文
                try:
                    return {"ok": True, "data": json.loads(raw)}
                except Exception:
                    return {"ok": True, "data": raw[:2000]}
            except Exception as e:
                return {"ok": False, "error": f"调用 {t.get('name')} 失败：{e}"}
    return {"ok": False, "error": f"未配置名为 '{tool}' 的 API 工具"}


# ---------- 工具注册（AI 可调用的能力）----------
# 内置工具：时间、天气占位（天气需要 API，未配置时返回提示）
BUILTIN_TOOLS = {
    "get_time": "返回当前本地时间",
    "weather": "查询天气（需在设置中配置天气 API）",
    "web_search": "联网搜索网页",
}


def ask_internet_search(query: str, top_k: int = 4) -> str:
    """把搜索行为转化为可注入的结果文本。"""
    results = web_search(query, n=top_k)
    lines = ["【联网搜索结果】"]
    for r in results[:top_k]:
        lines.append(f"- {r['title']} ({r['url']})")
        if r.get("desc"):
            lines.append(f"  {r['desc']}")
    return "\n".join(lines)