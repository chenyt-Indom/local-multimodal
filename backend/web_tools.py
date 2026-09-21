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
from datetime import date as _date, datetime as _dt

WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 主动声明只接受这两种 —— **别写 br**：本机没有 brotli 库，
    # 一旦服务器真按 br 返回就彻底读不出内容（宁可要未压缩的）。
    "Accept-Encoding": "gzip, deflate",
}


def _read_body(resp, max_bytes: int = 0) -> bytes:
    """读响应体，并按 Content-Encoding **自己解压**。

    ⚠️ 为什么必须自己解：urllib 不像 requests 那样自动解 gzip。
    实测 `https://www.python.org/downloads/` —— 我们没声明要压缩，
    但 nginx 仍然返回 `content-encoding: gzip`，于是
    `resp.read().decode('utf-8')` 拿到的是一堆乱码，
    `fetch_page_text` 还会把这堆乱码当正文交给模型（表面看是"能读"，其实全是废字符）。
    更糟的是它同时会让 `re.findall('<p>')` 全部落空，正文提取逻辑一起失效。

    brotli 只在装了库时才解，没装就返回空 —— 由调用方按"抓不到"处理，
    绝不能让乱码流到模型那里。
    """
    import gzip
    import zlib

    raw = resp.read(max_bytes) if max_bytes else resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
    if not enc or enc == "identity":
        return raw
    try:
        if "gzip" in enc or "x-gzip" in enc:
            try:
                return gzip.decompress(raw)
            except Exception:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except Exception:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        if "br" in enc:
            try:
                import brotli
                return brotli.decompress(raw)
            except Exception:
                return b""
    except Exception:
        # 解压失败时**不要**把压缩体当原文返回，否则又是一堆乱码。
        return b""
    return raw


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=WEB_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_body(resp).decode("utf-8", errors="ignore")


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
    """判断一条结果是否属于"搜索引擎自家的频道页 / 站点首页"这类噪音。"""
    url = (item.get("url") or "").strip()
    if not url.startswith("http"):
        return True
    low = url.lower()
    if any(t in low for t in _SELF_CHANNEL_JUNK):
        return True
    title = (item.get("title") or "")
    if any(t in title for t in _SELF_CHANNEL_TITLE):
        return True
    # 站点首页：路径为空或只有 /，标题又很短（如「Bilibili」「哔哩哔哩」）。
    # 这类条目对"查资料"毫无价值，只会占位置。
    try:
        path = urllib.parse.urlparse(url).path.strip("/")
    except Exception:
        path = ""
    if not path and len(title.strip()) <= 14:
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

    规则：
    1. 先留下「至少达到最佳结果的 40%，且绝对命中不低于 15%」的；
    2. 不足 keep_min 条时**有限度地**补位：只补相关度 ≥ 6% 的。
       绝不为了凑数把毫不相干的（站点首页、无关政策新闻）塞进来 ——
       那些进了来源清单只会让用户困惑，也会污染模型的材料。
    """
    if not items:
        return []
    scored = sorted(((relevance_ratio(query, it), it) for it in items),
                    key=lambda x: -x[0])
    best = scored[0][0]
    cutoff = max(best * 0.4, 0.15)
    kept = [it for r, it in scored if r >= cutoff]
    if len(kept) < keep_min:
        floor = max(cutoff * 0.3, 0.06)
        for r, it in scored:
            if len(kept) >= keep_min:
                break
            if r >= floor and it not in kept:
                kept.append(it)
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


# ---------- 网页正文抓取（深度阅读）----------
# 为什么需要：搜索给回来的只是 100~200 字的摘要，篇幅有限、信息密度低，
# 模型据此只能写出很短的回答。把前几条结果的**正文**抓下来喂给模型，
# 才有材料"总结 + 分析 + 展开"。
#
# 注意这是额外请求，必须：
#   · 并发抓取（串行会拖到十几秒）
#   · 每条设短超时 + 体积上限（有些页面几百 KB 甚至更多）
#   · 失败就静默跳过（很多站点有反爬/需要 JS，抓不到很正常）

_PAGE_SKIP_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".zip", ".rar", ".7z", ".mp4", ".mp3", ".apk", ".exe")
# 常见的正文容器（按优先级）；取不到就退回整页去标签
_PAGE_MAIN_PATTERNS = (
    r'<article[^>]*>(.*?)</article>',
    r'<div[^>]+class="[^"]*(?:article|content|main|detail|post|entry|body)[^"]*"[^>]*>(.*?)</div>',
    r'<main[^>]*>(.*?)</main>',
)


def fetch_page_text(url: str, limit: int = 1200, timeout: int = 8) -> str:
    """抓取网页正文纯文本（用于深度阅读）。失败返回空字符串。"""
    import re

    low = (url or "").lower()
    if not low.startswith("http") or any(low.split("?")[0].endswith(e) for e in _PAGE_SKIP_EXT):
        return ""
    try:
        req = urllib.request.Request(url, headers={
            **WEB_HEADERS,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            # 除了网页，也放行 json/xml —— 用户贴一个 API 地址时同样能读到内容
            # （之前只认 html/text，贴 https://api.github.com/... 一律返回"抓不到"）。
            if ctype and not any(k in ctype for k in
                                 ("html", "text", "json", "xml")):
                return ""
            # 上限 600KB（压缩体）→ 解压后再限 4MB，防止解压炸弹
            raw = _read_body(resp, 600 * 1024)[:4 * 1024 * 1024]
    except Exception:
        return ""
    if not raw:
        return ""

    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            html_txt = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        html_txt = raw.decode("utf-8", errors="ignore")

    # 去掉整块噪音
    html_txt = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", html_txt)
    html_txt = re.sub(r"(?is)<!--.*?-->", " ", html_txt)

    # 优先在正文容器里取
    best = ""
    for pat in _PAGE_MAIN_PATTERNS:
        for m in re.finditer(pat, html_txt, re.S):
            seg = _clean(m.group(1))
            if len(seg) > len(best):
                best = seg

    # 导航栏/侧边栏的链接文字往往被拼成一长串"首页 招生计划 历年分数 …"，
    # 信息密度极低却很长，容易盖过真正的正文。这里优先用**段落**拼正文：
    # <p> 通常是正文单位，而导航多是 <a>/<li>。
    paras = [_clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", html_txt)]
    paras = [p for p in paras if len(p) >= 20]
    if paras:
        joined = " ".join(paras)
        # 段落总长够用就用它（比整页干净得多）
        if len(joined) >= 150:
            best = joined if len(joined) > len(best) * 0.6 else best

    text = best if len(best) >= 120 else _clean(html_txt)

    # 合并空白并截断
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_pages(urls: list, limit: int = 1200, workers: int = 5,
                timeout: int = 8) -> dict:
    """并发抓取多个网页正文 → {url: text}。抓不到的条目直接不出现在结果里。"""
    from concurrent.futures import ThreadPoolExecutor

    urls = [u for u in dict.fromkeys(urls or []) if u]
    if not urls:
        return {}
    out = {}
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
            futs = {pool.submit(fetch_page_text, u, limit, timeout): u for u in urls}
            for f, u in futs.items():
                try:
                    t = f.result(timeout=timeout + 4)
                except Exception:
                    t = ""
                if t:
                    out[u] = t
    except Exception:
        pass
    return out



# ---------- 天气查询（结构化数据，不用搜索引擎）----------
# 为什么单独做：搜索引擎对"明天上海天气"只会返回「上海天气预报_15天」这类
# 导航页，拿不到任何真实数值。天气必须走数据 API。
#
# 数据源：**只用中国气象局**（经高德地图转发）—— 实况是气象站观测值、
# 预报是气象台产品，和你在国内天气 App 上看到的是同一套。
#
# ⚠️ 2026-09-18 用户明确要求：**Open-Meteo 完全不再使用**（连兜底也不要）。
#    它的问题实测留档，省得以后再纠结一遍：
#      · 逐日"下不下雨"（10 城 × 3 天对比）：**37% 虚报下雨、0 漏报**
#        —— 错法方向固定，只会把晴天说成毛毛雨，对出行判断最有害；
#      · 实况气温差 −2.8 ~ +2.6℃；体感温度湿热时虚高 5℃+（它是按公式自己算的）；
#      · 中文城市名解析弱：「汕头」不加"市"就查不到，「东京」会匹配到江苏的小地方。
#    代价（要如实告诉用户，不许模型自己编）：
#      高德**只给 4 天预报**，而且**没有体感温度、降水概率、降水量**。
#      format_weather 会在结果**最前面**写明缺哪些字段。


def _online() -> bool:
    """当前是否允许联网（跟着「联网」开关）。读不到就按"允许"处理，别把功能卡死。"""
    try:
        from . import map_tools as _mt
        return bool(_mt.online())
    except Exception:
        return True


def weather(city: str, days: int = 3) -> dict:
    """查天气 —— **只用中国气象局的数据**（经高德地图）。

    查不到时 ok=False，`error` 里写清楚为什么（没配 key / 境外 / 高德没有这个地名的数据）。
    ⚠️ **不回退到任何其它源**（用户明确要求）。
    """
    # ⚠️ 下限是 3，不是 1。实测踩过（2026-09-18）：用户问"明天汕头天气"，
    #    模型把 days 传成了 1（它理解成"我要 1 天的数据"），而下面是**从今天起**截断，
    #    结果正好把用户要的明天砍掉，模型只好回"本次没提供明天的预报"。
    #    数据源一次请求本来就返回 4 天，多留两行**零成本**，所以把下限兜住。
    days = max(3, min(int(days or 3), 16))
    if not _online():
        # 查天气必须联网。关着「联网」还去发请求，等于偷偷联网 —— 与开关的约定不符。
        return {"ok": False,
                "error": "离线模式查不了天气（要联网）。打开顶栏的「联网」开关就能查。"}
    try:
        from . import amap as _am
    except Exception as e:
        return {"ok": False, "error": "天气模块加载失败：%s" % str(e)[:60]}
    if not _am.key():
        # ⚠️ 没 key 时**别硬查**，直接告诉用户去配 —— 界面上有「高德 key」按钮，
        #    模型也可以调 connect_amap 弹窗请他填（填完立刻生效，不用重启）。
        return {"ok": False,
                "error": ("本机还没配高德 key，查不了天气。点界面顶栏的「高德 key」"
                          "填一个（免费，约 1 分钟）就能查了。")}
    try:
        w = _am.weather(city, days)
    except Exception as e:
        return {"ok": False, "error": "天气查询失败：%s" % str(e)[:80]}
    if w and w.get("ok"):
        return w
    if w and w.get("foreign"):
        return {"ok": False,
                "error": ("天气只覆盖**中国大陆**（数据来自中国气象局），"
                          "查不了「%s」这类境外地名；国内地名请带上城市，"
                          "例如「广东 汕头」。" % city)}
    return w or {"ok": False, "error": "天气查询失败：没拿到数据"}


def _fmt_n(v) -> str:
    """数字美化：28.0 → 28，28.6 → 28.6（缺值返回空串）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return ("%d" % f) if abs(f - round(f)) < 0.05 else ("%.1f" % f)


_WD_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _day_label(ds: str) -> str:
    """把日期写成「今天·周五」这样的标签。

    ⚠️ 必须**按真实日期算**，不能按列表下标（原来是 `["今天","明天","后天"][i]`）。
       下标算法一旦错位就全错 —— 比如某天数据少了一条、或首条不是今天
       （跨零点、数据源延迟都可能），模型就会**把明天说成今天**。
       日期算出来的标签永远和真实日历一致。
    """
    try:
        d = _dt.strptime(str(ds or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return ""
    wd = _WD_CN[d.weekday()]
    delta = (d - _date.today()).days
    rel = {0: "今天", 1: "明天", 2: "后天"}.get(delta)
    if rel is None and delta > 0:
        rel = "%d天后" % delta
    return (rel + "·" + wd) if rel else wd


def format_weather(w: dict) -> str:
    """把天气数据格式化成给模型看的紧凑文本。

    ⚠️ 中国气象局（经高德）**没有体感温度、降水概率、降水量**，风的单位还是
    "风力等级"（"东≤3"）不是 km/h —— 所以一律"有什么写什么"：
    缺的字段**绝不能打印出 None**（那会让模型讲成"体感 None 度"），
    而且要在**最前面**告诉模型"这些数据没有"，否则它会自己编（实测编出过「体感 29.5℃」）。
    """
    if not w.get("ok"):
        return f"天气查询失败：{w.get('error')}"
    where = w.get("city") or ""
    if w.get("admin"):
        where += "（%s）" % w["admin"]
    # ⚠️ 表头必须把**今天是几号**写出来：模型回答「明天/后天」时得先知道今天。
    #    实测用户问"明天汕头天气"，模型看到逐日里有 2026-09-19 却不敢认它就是"明天"，
    #    自己脑补出"本次没提供明天的预报"。给了锚点它才能对上号。
    lines = ["【%s 天气】数据源：%s　坐标 %.2f,%.2f"
             % (where, w.get("source", ""), w.get("lat") or 0, w.get("lon") or 0),
             "今天是 %s（%s）" % (_date.today().strftime("%Y-%m-%d"),
                                  _WD_CN[_date.today().weekday()])]
    c = w.get("current") or {}
    if c:
        bits = []
        if c.get("desc"):
            bits.append(str(c["desc"]))
        if c.get("temp") is not None:
            bits.append("气温 %s°C" % _fmt_n(c["temp"]))
        if c.get("feels") is not None:
            bits.append("体感 %s°C" % _fmt_n(c["feels"]))
        if c.get("humidity") is not None:
            bits.append("湿度 %s%%" % _fmt_n(c["humidity"]))
        if c.get("rain_mm") is not None:
            bits.append("降水 %s mm" % _fmt_n(c["rain_mm"]))
        # ⚠️ 风**照抄数据源自己的单位**（高德给的是"东≤3"这种风力等级，不是 km/h）
        if c.get("wind"):
            bits.append("风 %s" % c["wind"])
        if c.get("report_time"):
            bits.append("观测时间 %s" % c["report_time"])
        if bits:
            # ⚠️ 标题必须写明"只代表此刻、不是某天的预报"：实测（2026-09-18）模型问
            #    "明天天气"时，把这里的**湿度 72%** 直接搬进了"明天的预报"里 ——
            #    湿度/风力这些实况字段高德的逐日预报**没有**，搬过去就是编。
            lines.append("**此刻实况**（只代表现在这一刻，**不是**任何一天的预报）："
                         + "，".join(bits))
    lines.append("逐日预报：")
    for d in (w.get("daily") or []):
        tag = _day_label(d.get("date"))
        seg = ["%s%s %s" % (d.get("date", ""),
                            ("（%s）" % tag) if tag else "", d.get("desc", ""))]
        if d.get("low") is not None or d.get("high") is not None:
            seg.append("%s~%s°C" % (_fmt_n(d.get("low")), _fmt_n(d.get("high"))))
        if d.get("rain_pct") is not None:
            seg.append("降水概率 %s%%" % _fmt_n(d["rain_pct"]))
        if d.get("rain_mm") is not None:
            seg.append("降水 %s mm" % _fmt_n(d["rain_mm"]))
        if d.get("wind"):
            seg.append("风 %s" % d["wind"])
        # 混了两个源时逐日标出来，免得"前 4 天一个口径、后几天另一个口径"看着矛盾
        if d.get("src") and len({x.get("src") for x in (w.get("daily") or [])}) > 1:
            seg.append("（%s）" % d["src"])
        lines.append("  " + "　".join(seg))
    if w.get("note"):
        lines.append(f"说明：{w['note']}　回答时请注明数据来源。")

    # ⚠️⚠️ 缺什么就明说缺什么 —— 而且要**放在最前面**。
    #    实测（2026-09-18）：高德不给体感温度和降水概率，模型就自己编了
    #    「体感温度约 29.5°C」「降水概率 0%（无降雨）」，说得跟真的一样。
    #    弱模型对"结果末尾的附注"基本不看，放在开头才会照做（这条踩过好几次）。
    missing = []
    c0 = w.get("current") or {}
    if c0 and c0.get("feels") is None:
        missing.append("体感温度")
    d0 = (w.get("daily") or [{}])[0]
    if d0.get("rain_pct") is None:
        missing.append("降水概率")
    if d0.get("rain_mm") is None:
        missing.append("降水量")
    head = ""
    if missing:
        head = ("⚠️ 本次**没有**这些数据：%s —— 这个数据源不提供。"
                "**不许估算、不许编，连 0、未知 这类占位数字都不要写**；"
                "只能原样写「数据源不提供」。只能用下面真正出现的字段作答。\n\n"
                % "、".join(dict.fromkeys(missing)))
    return head + "\n".join(lines)


# ---------- 图片搜索（找现成的图，不是生成图）----------
IMG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    # 注意：这里**故意不写死 Referer**。
    # 以前写死 cn.bing.com，结果百度系图库（bkimg.cdn.bcebos.com 等）全部 403，
    # 用户看到的就是"搜图结果特别少"。Referer 由 download_image 按来源页动态给。
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


def _core_img_query(q: str) -> str:
    """把「捕蝇草 植物 高清」这类查询收缩到**最核心的一段**（首个词）。

    ⚠️ 为什么要收缩：图片搜索对"修饰词"的耐受度比文本搜索差得多。
    实测「捕蝇草 植物」在某个源上会返回一堆毫不相干的图，
    而单独搜「捕蝇草」就完全正常。搜索词越短越具体，越不容易被带偏。
    只在原查询**一条相关结果都拿不到**时才用它兜底，不改变首选查询。
    """
    import re
    parts = [p for p in re.split(r"[\s,，、/;；:：()（）\[\]【】]+", q or "")
             if len(p) >= 2]
    if len(parts) < 2:
        return ""
    core = max(parts, key=len)          # 取最长的一段（通常是主体名词）
    return "" if core == q else core


# --------------------------------------------------------------------------
# 图片源
# --------------------------------------------------------------------------
def _img_360(query: str, n: int) -> list:
    """360 图片（`image.so.com/j`，JSON 接口）—— **首选源**。

    ⚠️ 为什么把它放在第一位（2026-09-21 实测）：
    Bing 图片搜索在"无 JS 抓取"这个场景下**不稳定到不可用**。同一个接口：
      · 搜「捕蝇草」         → 35 条，全对；
      · 搜「捕蝇草 植物」   → 1 条，内容是"地暖保温条"；
      · 搜「维纳斯捕蝇草」 → 12 条，全是 Photoshop CS6 下载页；
      · 搜「捕蝇草 结构」   → 12 条，全是"世界旅游胜地"。
    加 Cookie、加 mkt/FORM 参数都无效（已逐一验证）。**返回的页面 title 是
    对的、结果区却是别的内容**，所以模型会拿这堆图当"用户的答案"去描述 ——
    比"搜不到"危害大得多。
    360 这边同两个查询分别返回 57 / 117 条，标题全部对得上，还附带宽高。

    ⚠️ 它的 `img` 直链在 `*.qhimg.com` CDN 上，**Referer 要给 image.so.com**。
    """
    import json as _json
    url = ("https://image.so.com/j?q=" + urllib.parse.quote(query)
           + f"&pn={max(int(n), 12)}&src=srp&sn=0")
    h = dict(IMG_HEADERS)
    h["Referer"] = "https://image.so.com/"
    h["Accept"] = "application/json, text/plain, */*"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(_read_body(resp).decode("utf-8", "ignore"))
    out = []
    for it in (data.get("list") or []):
        if not isinstance(it, dict):
            continue
        img = str(it.get("img") or it.get("https") or "").strip()
        if img.startswith("//"):
            img = "https:" + img
        if not img.startswith("http"):
            continue
        try:
            wid = int(it.get("width") or 0)
            hei = int(it.get("height") or 0)
        except Exception:
            wid = hei = 0
        out.append({
            "title": _clean(it.get("title") or ""),
            "url": img,
            "thumb": str(it.get("thumb") or it.get("thumb_bak") or "").strip(),
            "source": str(it.get("link") or "").strip() or ("https://" + str(it.get("site") or "").strip()),
            "referer": "https://image.so.com/",
            "width": wid, "height": hei,
        })
    return out


def _img_baidu(query: str, n: int) -> list:
    """百度图片（`image.baidu.com/search/acjson`，JSON 接口）—— 备用源。"""
    import json as _json
    url = ("https://image.baidu.com/search/acjson?tn=resultjson_com&logid=1"
           "&ipn=rj&ct=201326592&fp=result&word=" + urllib.parse.quote(query)
           + f"&pn=0&rn={max(int(n), 30)}")
    h = dict(IMG_HEADERS)
    h["Referer"] = "https://image.baidu.com/"
    h["Accept"] = "application/json, text/plain, */*"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(_read_body(resp).decode("utf-8", "ignore"))
    out = []
    for it in (data.get("data") or []):
        if not isinstance(it, dict):
            continue
        img = str(it.get("middleURL") or it.get("thumbURL") or "").strip()
        if not img.startswith("http"):
            continue
        out.append({
            "title": _clean(it.get("fromPageTitleEnc") or ""),
            "url": img,
            "thumb": str(it.get("thumbURL") or "").strip(),
            "source": str(it.get("fromURL") or it.get("fromURLHost") or "").strip(),
            "referer": "https://image.baidu.com/",
            "width": 0, "height": 0,
        })
    return out


def _img_bing(query: str, n: int) -> list:
    """Bing 图片搜索（HTML 抓取）—— **最后的兜底**，见 `_img_360` 的说明。"""
    import html as _html
    import re
    url = ("https://cn.bing.com/images/search?q=" + urllib.parse.quote(query)
           + f"&count={max(int(n) * 4, 24)}")
    page = _fetch(url)
    out = []
    for m in re.finditer(r'class="iusc"[^>]*m="([^"]+)"', page):
        try:
            data = json.loads(_html.unescape(m.group(1)))
        except Exception:
            continue
        murl = str(data.get("murl") or "").strip()
        if not murl.startswith("http"):
            continue
        purl = str(data.get("purl") or "").strip()
        out.append({
            "title": _clean(data.get("t") or ""),
            "url": murl,
            "thumb": str(data.get("turl") or "").strip(),
            "source": purl,
            # ⚠️ 防盗链：给图片所在页的成功率最高，给 cn.bing.com 反而 403
            "referer": purl or "https://cn.bing.com/",
            "width": 0, "height": 0,
        })
    return out


_IMG_SOURCES = (
    ("360图片", _img_360),
    ("百度图片", _img_baidu),
    ("Bing图片", _img_bing),
)


def _img_overlap(query: str, items: list) -> bool:
    """这批结果里，**有没有任何一条的标题真的提到了查询词**。

    ⚠️ 为什么用"标题里出现查询词"这么朴素的判据：
    图片搜索的标题通常就是来源网页的标题，正文里必然带着查询词
    （实测搜「捕蝇草」35 条标题里 35 条含"捕蝇草"）；
    而源返回垃圾时（Photoshop 下载页、地暖保温条、光学透镜图），
    标题与查询**零重合**。所以"零重合"就是"这个源这次给的东西不能用"的信号。

    ⚠️ 三个把判据做准的细节（都是踩出来的）：
    1. **只看标题，不看来源网址** —— URL 里满是随机字母数字，
       一个乱码查询的 2 字片断（"9f"/"qq"）能轻松在网址里命中，于是垃圾被判成"相关"；
    2. **词元要够具体**：优先用 3 字及以上的片断；只有 2 字片断可用时，
       要求**两个字都不是虚词**（"的词"这种必须排除，否则
       "形容心情不好的词语"会被当成乱码查询的命中结果）；
    3. 纯英文数字片断要求 3 位以上，同理排除 "9f" 这类碎片。
    """
    terms = _terms(query)
    if not terms:
        return True                      # 没啥可判的，别拦
    strong = [t for t in terms if len(t) >= 3]
    if not strong:
        strong = [t for t in terms if len(t) == 2
                  and not any(c in _LOW_INFO_CHARS for c in t)]
    if not strong:
        return True
    for it in items:
        title = str(it.get("title") or "")
        if title and any(t in title for t in strong):
            return True
    return False


def image_search(query: str, n: int = 4) -> list:
    """联网搜图：返回 [{title, url, thumb, source, referer, width, height}]。

    **与 generate_image 的区别**：这里找的是网上已存在的图片（原图直出），
    不做任何绘制；用户要求「找张图/搜张图/给我看看 xx 长什么样」时用它。

    取图策略（顺序即优先级）
    ------------------------
    1. 查询词：先清洗掉「照片/高清」等冗余词，**一条相关结果都拿不到时**
       再收缩到核心词重试一次（见 `_core_img_query`）；
    2. 源：360 → 百度 → Bing，**第一个给出可用结果的源就用它**，不再往下试；
    3. 每个候选批次都过一遍 `_img_overlap` —— 整批与查询零重合的直接丢弃，
       换下一个源；全部源都失败就返回空列表。

    ⚠️ 第 3 条是这套东西的**安全底线**：宁可返回空（模型会如实说"没搜到"），
    也绝不能把别的主题的图当成用户的答案交出去 —— 实测过"搜捕蝇草返回
    光学透镜示意图"，模型会照着图片内容去描述，用户看到的就是一本正经的错。
    """
    q = str(query or "").strip()
    if not q:
        return []
    want = max(int(n) * 3, 12)

    tries = []
    for cand in (_clean_img_query(q), _core_img_query(q)):
        if cand and cand not in tries:
            tries.append(cand)

    for cq in tries:
        for src_name, fn in _IMG_SOURCES:
            try:
                raw = fn(cq, want)
            except Exception:
                continue                 # 某个源抽风不影响别的源
            # 去重（同一张图在源里可能重复）
            seen, batch = set(), []
            for r in raw:
                u = r.get("url") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                r["engine"] = src_name
                batch.append(r)
            if not batch:
                continue
            if not _img_overlap(cq, batch):
                continue                 # 整批与查询零重合 → 这个源这次信不过
            out = _rank_images(cq, batch)[:n]
            if out:
                return out
    return []


def _rank_images(query: str, items: list) -> list:
    """按标题相关度排序，并把「零重合」的条目压在最后（有更好的就别给这些）。"""
    scored = []
    for i, it in enumerate(items):
        try:
            rel = relevance_ratio(query, it)
        except Exception:
            rel = 0.0
        scored.append((rel, i, it))
    scored.sort(key=lambda x: (-x[0], x[1]))
    good = [it for rel, _i, it in scored if rel > 0]
    if len(good) >= 1:
        return good
    return [it for _rel, _i, it in scored]


def _fetch_bytes(url: str, referer: str | None, max_bytes: int,
                 timeout: int) -> bytes | None:
    """取一次图片字节。HTTP 错误往外抛（调用方据此决定要不要换 Referer）。"""
    h = dict(IMG_HEADERS)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            return None                      # 拿到的是网页/错误页，不是图
        raw = resp.read(max_bytes + 1)
    return None if len(raw) > max_bytes else raw


def download_image(url: str, max_bytes: int = 8 * 1024 * 1024,
                   timeout: int = 25, referer: str | None = None) -> bytes | None:
    """下载图片原始字节（限制体积，避免大图拖垮前端）。

    ⚠️ **防盗链是这里最大的坑**（实测，也是"搜图结果很少"的根因）：
    同一个链接，`Referer` 给 `https://cn.bing.com/` 会 **403 Forbidden**，
    改成**图片所在页**或 `https://baike.baidu.com/` 就能正常拿到
    （实测同一张图：bing 的 Referer → 403；来源页 → 11248 字节，正常）。

    所以这里按「结果里带的 referer → 各图库的自家 referer → 不带」依次重试。
    只在 **401/403**（防盗链）时才换下一个 Referer ——
    404 之类换 Referer 也没用，网络超时更不该反复重试浪费时间。
    ⚠️ 360 的图挂在 `*.qhimg.com` 上，**认的是 `https://image.so.com/`**
    （不是图片所在页！），所以它必须在这个重试链里。
    """
    import urllib.error

    refs = [referer, "https://image.so.com/", "https://image.baidu.com/",
            "https://baike.baidu.com/", "https://cn.bing.com/", None]
    tried = set()
    for i, r in enumerate(refs):
        if r in tried:
            continue
        tried.add(r)
        try:
            # 第一次给足超时；后续重试只是换个 Referer，给短一点，别让失败拖慢整体
            return _fetch_bytes(url, r, max_bytes, timeout if i == 0 else min(timeout, 8))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                continue                     # 防盗链 → 换 Referer 再试
            return None                      # 404/410 等，换 Referer 也无用
        except Exception:
            return None                      # 网络问题，不反复重试
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
                    raw = _read_body(resp).decode("utf-8", errors="ignore")
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