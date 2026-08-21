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


def web_search(query: str, n: int = 5) -> list:
    """用 Bing 网页搜索，返回 [{title, url, desc}]。失败时返回带说明的条目。"""
    try:
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + f"&count={n}"
        req = urllib.request.Request(url, headers=WEB_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        return _parse_bing(query, html, n)
    except Exception as e:
        return [{"title": "搜索失败", "url": "", "desc": f"联网搜索出错：{e}"}]


def _parse_bing(query: str, html: str, n: int):
    """解析 Bing 结果页 HTML（尽量稳健，抓不到就返回简单列表）。"""
    results = []
    # 简单按 <li class="b_algo"> 分段
    import re
    blocks = re.split(r'<li class="b_algo"', html)[1:]
    for blk in blocks[:n]:
        title_m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', blk, re.S)
        if not title_m:
            continue
        url = title_m.group(1).strip()
        title = re.sub(r"<[^>]+>", "", title_m.group(2)).strip()
        desc_m = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip() if desc_m else ""
        results.append({"title": title, "url": url, "desc": desc[:300]})
    # 兜底：若正则没抓到，至少返回关键词相关提示
    if not results:
        results = [{"title": "（链接解析失败，可复制下面地址在浏览器打开）",
                    "url": "https://www.bing.com/search?q=" + urllib.parse.quote(query), "desc": ""}]
    return results


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