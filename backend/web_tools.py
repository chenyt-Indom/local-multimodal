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
    """360 搜索（<h3 class="g-title"> 结构）。"""
    import re
    html = _fetch("https://www.so.com/s?q=" + urllib.parse.quote(query))
    out = []
    pattern = re.compile(
        r"<h3[^>]*class=\"[^\"]*g-title[^\"]*\"[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
        re.S,
    )
    for m in pattern.finditer(html):
        if len(out) >= n:
            break
        title = _clean(m.group(2))
        if len(title) < 3:
            continue
        url = m.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        out.append({"title": title, "url": url, "desc": "", "engine": "360"})
    return out


def _search_sogou(query: str, n: int) -> list:
    """搜狗搜索（结果标题在 <h3 class="vr-title"> 内）。"""
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


def _merge_engines(batches: list, n: int) -> list:
    """轮询合并各引擎结果并去重，保证来源多样。"""
    import re
    seen, merged, idx = set(), [], 0
    while len(merged) < n:
        added = False
        for batch in batches:
            if idx < len(batch):
                item = batch[idx]
                key = re.sub(r"\W+", "", item.get("title", ""))[:18]
                if key and key not in seen:
                    seen.add(key)
                    merged.append(item)
                    added = True
                    if len(merged) >= n:
                        break
        if not added and all(idx >= len(b) for b in batches):
            break
        idx += 1
    return merged


# 可用的检索引擎（并行调用，任一成功即可返回结果）
# 注意：实测 **国际版 Bing 对中文查询质量极差**（会把"广州民航职业技术学院"
# 匹配成"豆包输入法"等无关内容），因此不纳入，只用中文友好的三个源。
_ENGINES = (_search_bing, _search_360, _search_sogou)


def _keyword_hits(query: str, item: dict) -> int:
    """统计一条结果命中查询关键词的个数。"""
    import re
    words = [w for w in re.split(r"[\s,，、/]+", query) if len(w) >= 2]
    if not words:
        return 1
    text = (item.get("title", "") or "") + " " + (item.get("desc", "") or "")
    return sum(1 for w in words if w in text)


def filter_relevant(query: str, items: list) -> list:
    """过滤掉与查询明显无关的结果（一条都没命中关键词的丢弃）。

    实测场景：搜"广州民航职业技术学院 公办民办"时，引擎会返回
    「广州市_百度百科」「百度地图」等泛化结果，必须剔除，否则模型会被误导。
    """
    if not items:
        return []
    kept = [it for it in items if _keyword_hits(query, it) >= 1]
    return kept


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

    merged = _merge_engines(batches, n)
    if merged:
        return merged

    # 全部失败时的兜底：给出可点击的搜索地址
    q = urllib.parse.quote(query)
    return [{"title": "（未获取到搜索结果，可点开下列链接自行查看）",
             "url": f"https://cn.bing.com/search?q={q}",
             "desc": f"备选：https://www.so.com/s?q={q}",
             "engine": "fallback"}]


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