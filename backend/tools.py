# -*- coding: utf-8 -*-
"""Agent 工具注册表 —— 让 Qwen 具备主动调用能力。

工具分三类：
- 文生图：generate_image（对话中让大模型调用；中文 prompt 自动翻译成英文解决偏离问题）
- 文件系统：list_directory / read_file / search_files / write_file / append_file
- 记忆：memory_store / memory_search

每个工具执行后返回一段文本给模型（模型据此继续思考），
若有需要前端展示的副作用（例如生成的图片），则推入 ui_events，
由后端流式传给前端渲染。
完全本地运行。
"""
from __future__ import annotations
import os
import time
import glob

from . import t2i
from . import file_tools
from . import memory as memory_mod


# =====================================================================
#  工具 Schema（发给模型）
# =====================================================================
def make_schemas(web_enabled: bool = False) -> list:
    """返回工具 schema 列表。

    web_enabled=True 时才暴露联网搜索工具——保证"开关不开不联网"的约定：
    关着的时候模型连工具都看不到，自然不会去联网。
    """
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "web_image_search",
                "description": (
                    "【联网搜图】到网上找**已经存在**的真实图片，并把原图展示给用户。\n"
                    "★ 什么时候用它：用户说「找张……的图」「搜一下……图片」「……长什么样」"
                    "「来点……壁纸/照片」「有没有……的图片」——即用户想要看真实存在的图片。\n"
                    "★ 与 generate_image 的区别（务必分清）：\n"
                    "  · web_image_search = 搜索互联网上已有的真实照片/图片，不绘制（找现成的）\n"
                    "  · generate_image   = AI 从零画一张不存在的图（造新的）\n"
                    "  例：「找一张埃菲尔铁塔的照片」→ 本工具；"
                    "「画一只穿宇航服的柯基」→ generate_image。\n"
                    "若用户说的是「画/生成/绘制」，用 generate_image，不要用本工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，用中文或英文的名词短语，如「埃菲尔铁塔 照片」「橘猫 壁纸」"},
                        "n": {"type": "integer", "description": "返回图片数量，默认 4，最多 6"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_image_to_library",
                "description": "把刚刚搜到/生成的一张图片保存进本地图片库，供以后随时调用。一般在用户说「保存这张」「存起来」「收进图库」时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "要保存的是本轮展示的第几张图（从 1 开始），默认 1"},
                        "name": {"type": "string", "description": "保存后的名称，可选，留空自动命名"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "根据描述生成一张图片（文生图）。prompt 必须写成详细、具体的英文（例如 a cute corgi wearing astronaut helmet, futuristic city background），不要用中文。生成后会直接在界面展示给用户。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "详细的英文图片描述（SDXL 风格 prompt，英文）"},
                        "negative_prompt": {"type": "string", "description": "英文负面描述，可选，例如 'low quality, blurry, watermark'"},
                        "size": {"type": "integer", "enum": [512, 768], "description": "图片边长，默认512"},
                        "hd": {"type": "boolean", "description": "是否高清放大（默认 false）。当用户要求「高清/高分辨率/4K/画质好点/放大」时设为 true：会额外做 4 倍超分（512→2048），耗时约多 5 秒"},
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_image",
                "description": "对一张已存在的图片进行微改（图生图）。适用于用户给了/引用了一张图、希望局部修改，例如「把背景改成夜晚」「给猫戴上帽子」「换成红色」。source 可填本地图片路径；若用户本轮拖入一张图片要求微改，则不填 source，直接用该图片。prompt 用英文描述需要的修改，并尽量注明保持其他部分不变。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "英文描述要做的修改，含 keep the rest unchanged 之类约束"},
                        "source": {
                            "type": "string",
                            "description": "（可选）本地图片绝对路径；不填则使用用户本轮拖入对话的那张图"},
                        "negative_prompt": {
                            "type": "string",
                            "description": "英文负面描述，可选，例如 'low quality, blurry, distorted'"},
                        "strength": {
                            "type": "number",
                            "description": "修改强度 0~1，默认0.6；0.3=轻微微调，0.8=大改"},
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "列出指定目录下的一级内容（文件/子目录）。用于浏览用户本机文件系统。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录绝对路径，例如 C:\\Users\\xxx\\Documents"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取用户本机指定路径的文件内容（文本/代码/PDF等）。仅当用户给出本地文件路径、需要读取该文件时才调用。注意：图片/视频如果已经附在对话中（用户拖入/上传），直接用视觉能力观看即可，不要为看图调用本工具；若用户提供一个视频文件路径要分析内容，才用本工具抽帧。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "在指定目录（递归）或单文件中按关键词搜索匹配的内容片段。用于帮用户在本机查找资料。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目录或文件绝对路径"},
                        "keyword": {"type": "string", "description": "要搜索的关键词"},
                    },
                    "required": ["path", "keyword"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "写入或覆盖创建用户本机的一个文本文件。返回写入结果。可用于创建/写入代码、文档等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "content": {"type": "string", "description": "要写入的完整文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "append_file",
                "description": "向已有文本文件末尾追加内容（不会覆盖原有内容），若无该文件则新建。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "content": {"type": "string", "description": "要追加的文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remember",
                "description": "把用户透露的稳定事实/偏好/重要信息写入长期记忆的文段。记忆按【分区文段】组织（如 工作背景/个人背景/当前关注/近期动态）。只有值得长期记住的稳定信息才调用；临时问答、寒暄、一次性指令不要存。每次调用需给出 section（分区标题）和本次要写入的这一分区的最新整段文字 content——请基于该分区已有内容 + 本轮新信息重写合并后的完整文段（若该分区此前无内容则直接写新文段）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "分区标题，如 工作背景 / 个人背景 / 当前关注 / 近期动态（可新建其他贴切标题）"},
                        "content": {
                            "type": "string",
                            "description": "这一分区要存储的完整、最新的文段（第三人称、直接、可读，合并旧内容与新信息）"},
                    },
                    "required": ["section", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "检索我已拥有的长期记忆和过往对话，把最相关的信息读出来，以便回答用户（例如“我之前想让你……”“你还记得……吗”）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要检索的查询"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "返回当前本地日期与时间。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    if web_enabled:
        schemas.append(_WEATHER_SCHEMA)   # 天气走数据 API，比搜索可靠得多
        schemas.append(_WEB_SEARCH_SCHEMA)
    return schemas


# 天气工具：走结构化数据源，不要用搜索引擎
_WEATHER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "查询某地天气（实时 + 未来逐日预报）。"
            "**凡涉及天气的一律用这个工具，不要用 web_search** —— "
            "搜索引擎对天气查询只会返回「XX天气预报_15天」这类网站导航页，"
            "拿不到任何真实温度数值；本工具直接返回气温、降水概率、风速等数据。"
            "支持中文城市名（含地级市、县市）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，如「北京」「上海」「深圳」「乌鲁木齐」。可带省份消歧：「广东 深圳」。",
                },
                "days": {
                    "type": "integer",
                    "description": "预报天数，默认 3（含今天）。最多 16 天。",
                },
            },
            "required": ["city"],
        },
    },
}


# 联网搜索工具（仅当用户在前端打开"联网"开关时才注入给模型）
_WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索互联网上的最新信息。凡是涉及**实时或最新信息**的问题都应主动调用，例如："
            "最新新闻、时事热点、近期发生的事件、实时数据（股价/汇率/比分）、"
            "你不确定或知识可能过时的内容、需要查证的事实、某个新产品/新版本的现状等。"
            "调用时会真的联网检索并把网页摘要返回给你，你再据此总结成答案。"
            "**注意：查天气请改用 get_weather 工具**（搜索引擎给不出真实温度数值）。"
            "查某单位/机构的公开信息（性质、地址、招生、公开招聘、年报等）可配合下面两种写法："
            "在关键词里带上机构**全称**效果最好（如「广州民航职业技术学院 招生章程」）；"
            "想限定官方来源时可用 site: 语法（如「深圳大学 site:edu.cn」）。"
            "注意：日常闲聊、写作、翻译、代码等不需要联网的任务不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "搜索关键词。**务必精炼**：用 2~4 个核心词，不要写成完整句子，"
                        "也不要塞入具体年月日（加了反而容易被搜索引擎带偏，返回日历类无关结果）。"
                        "**中文提问请优先用中文关键词搜索**（中文资料的召回明显更好），"
                        "只有查国外产品/英文资料时才用英文。"
                        "示例（好）：「人工智能 最新进展」「英伟达 股价」「广州民航职业技术学院」；"
                        "示例（差）：「2026年9月人工智能领域有哪些重要进展」（太长且含日期）"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果条数，默认 8（多引擎聚合，可适当调大）",
                },
            },
            "required": ["query"],
        },
    },
}


# =====================================================================
#  工具执行
# =====================================================================
def _ui(events, event):
    """收集要推送给前端展示的副作用事件（直接存原始事件，由主循环包 {"ui":event}）。"""
    events.append(event)


def _tool_call_msg(name, args) -> str:
    args_s = json_dumps(args)
    return f"[工具已调用] {name}({args_s})"


def json_dumps(o) -> str:
    try:
        return __import__("json").dumps(o, ensure_ascii=False)
    except Exception:
        return str(o)


def dispatch(name: str, arguments: dict, ui_events: list, context: dict) -> str:
    """执行一个工具调用，返回给模型的文本。ui_events 收集前端副作用。"""
    if name == "web_image_search":
        return _do_web_image_search(arguments, ui_events)
    if name == "get_weather":
        return _do_get_weather(arguments)
    if name == "save_image_to_library":
        return _do_save_image_to_library(arguments, context)
    if name == "generate_image":
        return _do_generate_image(arguments, ui_events)
    if name == "edit_image":
        return _do_edit_image(arguments, ui_events, context)
    if name == "list_directory":
        return _do_list_directory(arguments)
    if name == "read_file":
        return _do_read_file(arguments, ui_events)
    if name == "search_files":
        return _do_search_files(arguments)
    if name == "write_file":
        return _do_write_file(arguments)
    if name == "append_file":
        return _do_append_file(arguments)
    if name == "remember":
        return _do_remember(arguments)
    if name == "search_memory":
        return _do_search_memory(arguments)
    if name == "get_time":
        return time.strftime("%Y-%m-%d %H:%M:%S (%A)")
    if name == "web_search":
        return _do_web_search(arguments, ui_events)
    return f"[未知工具] {name}"


# ---------- 联网搜索 ----------
# 查询里常见的"干扰词"：搜索引擎对这类限定词很敏感，会大幅降低召回质量
_NOISE_PATTERNS = [
    r"是公办还是民办", r"公办还是民办", r"公办\s*民办", r"是公办的吗", r"是民办的吗",
    r"是什么", r"怎么样", r"有哪些", r"是多少", r"为什么", r"怎么回事",
    r"的参数", r"参数配置", r"规格", r"详细介绍", r"介绍一下", r"请问",
]


def _simplify_query(q: str) -> str:
    """去掉年份/月日/疑问词/限定词，得到更通用的检索词。

    实测：搜索引擎对"广州民航职业技术学院 公办 民办"这类长查询召回很差，
    而只留主体词"广州民航职业技术学院"时能正常返回官网与百科。
    """
    import re
    s = q
    s = re.sub(r"\d{4}\s*年", " ", s)
    s = re.sub(r"\d{1,2}\s*月", " ", s)
    s = re.sub(r"\d{1,2}\s*日", " ", s)
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s)
    s = re.sub(r"[？?！!。，,、；;：:]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedupe(items: list) -> list:
    """按标题前 18 个有效字符去重。"""
    import re
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it.get("title", ""))[:18]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _relevant(query: str, results: list) -> bool:
    """粗略判断检索结果是否与查询相关（看实词命中比例）。"""
    import re
    words = [w for w in re.split(r"[\s,，、/]+", query) if len(w) >= 2]
    if not words:
        return True
    text = " ".join((r.get("title", "") + " " + r.get("desc", "")) for r in results)
    hits = sum(1 for w in words if w in text)
    return hits >= max(1, len(words) // 3)


def _do_web_search(arguments, ui_events):
    """联网检索：多引擎 + 多查询变体聚合 + 相关性过滤，尽量拿到可用结果。"""
    from concurrent.futures import ThreadPoolExecutor

    query = (arguments.get("query") or "").strip()
    if not query:
        return "联网搜索失败：未提供 query。"
    try:
        top_k = int(arguments.get("top_k") or 8)
    except Exception:
        top_k = 8
    top_k = max(1, min(top_k, 15))

    from . import web_tools

    # 查询变体：原查询 + 精简主体词（+ 机构类查询追加"官方站定向"）
    variants = [query]
    simple = _simplify_query(query)
    if simple and simple != query and len(simple) >= 2:
        variants.append(simple)

    # 查"某单位对外公开情况"时，普通检索会被百科/聚合站/同名地名淹没。
    # 这里额外跑一次 `主体词 site:gov.cn`（或 edu.cn / org.cn）定向检索，
    # 结果基本就是官网本身。实测对机构类查询的提升最明显。
    body = simple or query
    official_hint = web_tools.official_site_hint(body)

    jobs = [(v, "web") for v in variants]
    if official_hint:
        jobs.append((body, "official"))

    def _run(job):
        q, kind = job
        if kind == "official":
            return web_tools.search_official(q, top_k)
        return web_tools.web_search(q, n=top_k)

    try:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            batches = list(pool.map(_run, jobs))
    except Exception as exc:
        return f"联网搜索失败：{exc}"

    # 逐变体做相关性过滤（剔除"广州市_百度百科"这类泛化无关结果）
    filtered = []
    for (q, kind), b in zip(jobs, batches):
        filtered.append(web_tools.filter_relevant(
            q, b, keep_min=2 if kind == "official" else 3))

    # 组装优先级：原查询 → 官方站定向 → 精简查询
    order = [0] + [i for i, (_, k) in enumerate(jobs) if k == "official"] \
        + [i for i in range(1, len(jobs)) if jobs[i][1] != "official"]
    results, seen_idx = [], set()
    for i in order:
        if i in seen_idx or i >= len(filtered):
            continue
        seen_idx.add(i)
        results = _dedupe(results + filtered[i])
        if len(results) >= max(top_k, 8):
            break
    results = results[:max(top_k, 8)]

    # 相关度偏低时，明确告诉模型"这次检索不可靠"，避免它硬编内容
    confidence = web_tools.best_relevance(query, results)

    used = f"{query}（含精简检索：{simple}）" if len(variants) > 1 else query
    if official_hint:
        used += f"；已定向官方站 site:{official_hint}"

    # 输出格式刻意紧凑：长 URL 和超长摘要会挤占模型上下文、降低其理解质量，
    # 因此来源只给域名，摘要截断到 140 字。
    lines = [f"【联网搜索结果】检索词：{used}"]
    useful = 0
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("desc") or "").strip()
        if title and title not in ("搜索失败",) and "未获取到搜索结果" not in title and url:
            useful += 1
        lines.append(f"[{i}] {title}")
        if desc:
            lines.append(f"    摘要：{desc[:140]}")
        if url:
            # 给**完整链接**（此前只给域名，导致模型无法提供可点击来源）
            lines.append(f"    链接：{url}")
    if useful == 0:
        lines.append("（未获取到有效搜索结果。请如实告诉用户本次联网检索失败，不要编造内容；"
                     "可以建议用户换个更具体的说法再试。）")
    else:
        if confidence < 0.3:
            lines.append("（注意：本次检索结果与问题的匹配度较低，可能没有命中要点。"
                         "请如实说明「未检索到直接相关信息」，并建议用户换个关键词或提供更具体的名称，"
                         "不要用这些弱相关结果硬凑答案。）")
        lines.append(
            "请基于以上检索结果用中文总结回答，并严格遵守：\n"
            "1. 关键结论后用 [序号] 标注来源，例如「……[2]」；\n"
            "2. 回答最后单独起一段「信息来源」，每条一行，格式："
            "序号. 标题 — 完整链接（链接必须原样复制上面的「链接：」内容）；\n"
            "3. 只能引用上面真实出现过的条目，**绝不编造链接或来源**；\n"
            "4. 若结果不足以回答，如实说明局限，不要凭空补充。\n"
            "注意：当前时间以系统提示中的时间为准。")
    return "\n".join(lines)


# ---------- 文生图 ----------
_IMAGE_PROMPT_BOOST = (
    "professional photography, highly detailed, sharp focus, "
    "vivid colors, 8k, cinematic lighting, masterpiece, best quality"
)


# ---------- 联网搜图（找现成的真实图片，区别于文生图）----------
def _do_get_weather(arguments):
    """查天气：走结构化数据 API，不经过搜索引擎。"""
    from . import web_tools
    city = (arguments.get("city") or "").strip()
    if not city:
        return "请提供要查询的城市名。"
    try:
        days = int(arguments.get("days") or 3)
    except Exception:
        days = 3
    return web_tools.format_weather(web_tools.weather(city, days))


def _do_web_image_search(arguments, ui_events):
    """联网搜索真实图片，下载原图后展示给前端。"""
    import base64 as _b64
    from . import web_tools

    query = (arguments.get("query") or "").strip()
    if not query:
        return "联网搜图失败：未提供搜索关键词（query）。"
    try:
        n = int(arguments.get("n") or 4)
    except Exception:
        n = 4
    n = max(1, min(n, 6))

    try:
        results = web_tools.image_search(query, n=n)
    except Exception as exc:
        return f"联网搜图失败：{exc}"
    if not results:
        return "联网搜图没有找到相关图片。可以换个更通用的关键词再试。"

    shown, lines = 0, [f"【联网搜图】关键词：{query}"]
    for r in results:
        if shown >= n:
            break
        raw = web_tools.download_image(r["url"])
        if not raw:
            continue
        shown += 1
        mime = "image/png"
        if raw[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif raw[:4] == b"RIFF":
            mime = "image/webp"
        elif raw[:3] == b"GIF":
            mime = "image/gif"
        _ui(ui_events, {
            "type": "image", "mime": mime,
            "b64": _b64.b64encode(raw).decode("utf-8"),
            "prompt": r.get("title") or query,
            "url": r.get("url") or "",
            "source": r.get("source") or "",
            "origin": "web",              # ← 前端据此标注「网上搜到的」
        })
        lines.append(f"[{shown}] {r.get('title') or '(无标题)'}")
        if r.get("source"):
            lines.append(f"    来源页：{r['source']}")
        lines.append(f"    图片直链：{r['url']}")

    if shown == 0:
        return "联网搜图失败：找到了结果但图片下载不下来（可能被目标站点防盗链拦截）。"

    lines.append(
        f"已把 {shown} 张**网上搜索到的真实原图**展示给用户（这是搜索结果，不是你画的）。"
        "请用中文简要说明找到了什么内容，并提示：可点图片下方「保存到图库」留存。")
    return "\n".join(lines)


def _do_save_image_to_library(arguments, context):
    """把本轮展示过的某张图片存入本地图片库。"""
    from . import image_library

    try:
        idx = max(1, int(arguments.get("index") or 1))
    except Exception:
        idx = 1

    pool = (context or {}).get("shown_images") or []
    if not pool:
        return "保存失败：本轮还没有展示过任何图片。请先搜图或生成图片再来保存。"
    if idx > len(pool):
        return f"保存失败：本轮只展示了 {len(pool)} 张图，不存在第 {idx} 张。"

    item = pool[idx - 1]
    meta = image_library.save_image(
        item.get("b64") or item.get("url") or "",
        name=arguments.get("name") or "",
        source=item.get("source") or item.get("prompt") or "",
        origin=item.get("origin") or "web")
    if not meta.get("ok", True):
        return f"保存失败：{meta.get('error')}"
    return (f"已保存第 {idx} 张图到图片库，名称为「{meta['name']}」（id={meta['id']}）。"
            "用户可在左侧「图片库」面板随时查看、调用或删除。")


def _do_generate_image(arguments, ui_events):
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "错误：未提供图片描述（prompt）。"
    negative = (arguments.get("negative_prompt") or "").strip() or "low quality, blurry, watermark, text, deformed"
    size = int(arguments.get("size") or 512)
    hd = bool(arguments.get("hd"))
    # 提升 SD 对 prompt 的遵循度：追加质量词
    boosted = prompt + ", " + _IMAGE_PROMPT_BOOST
    t2i.unload()  # 确保显存空闲
    start = time.time()
    result = t2i.generate(boosted, negative_prompt=negative, steps=4,
                          width=size, height=size, hd=hd)
    cost = time.time() - start
    if not result.get("ok"):
        return (f"图片生成失败：{result.get('error')}。"
                f"请把冒号后的具体原因**原样**转告用户，不要改写成笼统说法。")
    # 把图片作为副作用发给前端展示；只把简短文本回给模型，避免占用上下文
    _ui(ui_events, {"type": "image", "mime": "image/png", "b64": result["b64"],
                    "prompt": prompt, "device": result.get("device"),
                    "model": result.get("model"), "cost_s": round(cost, 1),
                    "size": result.get("size"),
                    "origin": "gen"})     # ← 前端据此标注「AI 生成」
    real_size = result.get("size") or f"{size}x{size}"
    extra = f"（{result['hd_note']}）" if (hd and result.get("hd_note")) else ""
    return (f"已生成图片（{real_size}，{result.get('device')}，用 {round(cost,1)} 秒）{extra}。"
            f"生成的图片已经展示给用户。若用户想调整，可再次明确修改描述。")


def _do_edit_image(arguments, ui_events, context):
    """图片微改（图生图）：拿到一张参考图 + 修改描述，产出新图。"""
    import base64 as _b64
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "错误：未提供修改描述（prompt）。"
    negative = (arguments.get("negative_prompt") or "").strip() or \
        "low quality, blurry, watermark, distorted, deformed"
    strength = max(0.0, min(1.0, float(arguments.get("strength") or 0.6)))
    steps = int(arguments.get("steps") or 4)
    source = (arguments.get("source") or "").strip()

    # 1) 解析参考图：优先用 source 路径；否则用本轮对话拖入的那张图
    init_image = None
    if source:
        if not os.path.isfile(source):
            return f"错误：source 不是有效图片路径：{source}"
        init_image = source  # edit_image 支持路径
    else:
        imgs = (context or {}).get("images") or []
        if imgs:
            raw = imgs[0]
            if isinstance(raw, str) and "," in raw and raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            try:
                init_image = _b64.b64decode(raw)
            except Exception:
                init_image = None
    if init_image is None:
        return ("错误：无法确定要修改的图片。请把要改的图片拖入对话（作为本轮附件）后再让我微改，"
                "或通过 source 指定本地图片路径。")

    # 2) 提示措辞：强化"保持其余不变"
    boosted = prompt + ", keep the original layout and style, high detail"
    t2i.unload()
    start = time.time()
    result = t2i.edit_image(init_image, boosted, negative_prompt=negative,
                            steps=steps, strength=strength)
    cost = time.time() - start
    if not result.get("ok"):
        return (f"图片微改失败：{result.get('error')}。"
                f"请把冒号后的具体原因**原样**转告用户，不要改写成「暂时无法使用」这类"
                f"笼统说法 —— 用户需要看到真实原因才能判断问题在哪。")
    _ui(ui_events, {"type": "image", "mime": "image/png", "b64": result["b64"],
                    "prompt": "微改：" + prompt, "device": result.get("device"),
                    "model": result.get("model"), "cost_s": round(cost, 1)})
    return (f"已根据修改要求生成新图（用 {round(cost,1)} 秒）。原图已按描述微改并展示给用户。"
            f"若还要继续调整，请直接说明新的修改点。")


# ---------- 文件系统 ----------
def _do_list_directory(arguments):
    path = arguments.get("path") or ""
    if not path:
        return "错误：未提供目录路径。"
    if not os.path.isdir(path):
        return f"错误：不是有效目录：{path}（若它是文件，请用 read_file）"
    try:
        items = sorted(os.listdir(path))
    except PermissionError as e:
        return f"错误：无权限访问该目录：{e}"
    lines = []
    for it in items:
        full = os.path.join(path, it)
        kind = "[目录]" if os.path.isdir(full) else "  文件"
        try:
            size = os.path.getsize(full) if os.path.isfile(full) else ""
        except Exception:
            size = ""
        size_s = f"{size:,}B" if isinstance(size, int) else ""
        lines.append(f"{kind} {it} {size_s}")
    head = "\n".join(lines[:300])
    if len(lines) > 300:
        head += f"\n……（共 {len(lines)} 项，仅显示前 300 项）"
    return f"目录 {path} 的内容：\n{head}"


_VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts"}


def _do_read_file(arguments, ui_events):
    path = arguments.get("path") or ""
    if not path:
        return "错误：未提供文件路径。"
    # 视频：抽帧展示（让模型能“看”视频）
    if os.path.splitext(path)[1].lower() in _VIDEO_EXTS:
        from . import video as video_mod
        res = video_mod.extract_frames(path)
        if not res.get("ok"):
            return f"错误：{res.get('error')}"
        frames = res.get("frames", [])
        for i, b64 in enumerate(frames):
            _ui(ui_events, {"type": "image", "mime": "image/jpeg", "b64": b64,
                            "from": path, "frame": i + 1, "total": len(frames)})
        return (f"已读取视频并抽取 {len(frames)} 个关键帧展示给用户"
                f"（时长约{res.get('duration')}秒）。请综合这些画面描述视频内容。")
    result = file_tools.read_file(path)
    if not result.get("ok"):
        return f"错误：{result.get('error')}"
    if result.get("type") == "image":
        _ui(ui_events, {"type": "image", "mime": "image/jpeg", "b64": result["b64"], "from": path})
        return f"已读取图片并展示给用户：{path}。请基于这张图片回答。"
    # 文本/目录
    if result.get("type") == "dir":
        return f"这是目录，内容如下：\n{result.get('content','')}"
    text = result.get("content", "")
    if len(text) > 6000:
        text_short = text[:6000]
        return f"文件 {path} 的内容（截取前6000字符，共{len(text)}字符）：\n{text_short}"
    return f"文件 {path} 的内容：\n{text}"


def _do_search_files(arguments):
    path = arguments.get("path") or ""
    keyword = (arguments.get("keyword") or "").strip()
    if not path or not keyword:
        return "错误：需要同时提供 path 与 keyword。"
    if os.path.isfile(path):
        paths = [path]
    elif os.path.isdir(path):
        paths = []
        try:
            for ext in file_tools.TEXT_EXTS:
                paths += glob.glob(os.path.join(path, "**", "*" + ext), recursive=True)
        except Exception as e:
            return f"错误：扫描目录失败：{e}"
        paths = paths[:200]
    else:
        return f"错误：路径不存在：{path}"
    hits = []
    kws = [k.lower() for k in keyword.split()]
    for p in paths[:200]:
        text = file_tools.read_text(p)
        if not text:
            continue
        low = text.lower()
        if kws and any(k in low for k in kws):
            idx = min((low.find(k) for k in kws if k in low), default=0)
            seg = text[max(0, idx - 80): idx + 220].replace("\n", " ")
            hits.append(f"--- {p} ---\n…{seg}…")
    if not hits:
        return f"在 {path} 下未找到包含“{keyword}”的文件。"
    return "找到的匹配内容（最多返回30条）：\n\n" + "\n\n".join(hits[:30])


def _do_write_file(arguments):
    path = arguments.get("path") or ""
    content = arguments.get("content") or ""
    if not path:
        return "错误：未提供文件路径。"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        size = os.path.getsize(path)
        return f"已写入文件：{path}（{size} 字节）"
    except Exception as e:
        return f"写入失败：{e}"


def _do_append_file(arguments):
    path = arguments.get("path") or ""
    content = arguments.get("content") or ""
    if not path:
        return "错误：未提供文件路径。"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return f"已追加内容到：{path}"
    except Exception as e:
        return f"追加失败：{e}"


# ---------- 记忆工具 ----------
def _do_remember(arguments):
    section = (arguments.get("section") or "").strip()
    content = (arguments.get("content") or "").strip()
    if not content:
        return "错误：内容为空。"
    memory_mod.remember(section, content)
    return f"已更新长期记忆文段【{section}】。"


def _do_search_memory(arguments):
    query = arguments.get("query") or ""
    hits = memory_mod.search_all(query, top_k=5)
    if not hits:
        return "没有找到相关记忆信息。"
    lines = ["检索到的历史记忆："]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h.get('level','?')}]{h.get('content','')}")
    return "\n".join(lines)