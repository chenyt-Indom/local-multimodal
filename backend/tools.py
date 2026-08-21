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
def make_schemas() -> list:
    return [
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
                "description": "读取用户本机任意文件的内容（文本/代码/图片/PDF/视频抽帧）。图片和视频会直接展示，文本返回内容。",
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
    return f"[未知工具] {name}"


# ---------- 文生图 ----------
_IMAGE_PROMPT_BOOST = (
    "professional photography, highly detailed, sharp focus, "
    "vivid colors, 8k, cinematic lighting, masterpiece, best quality"
)


def _do_generate_image(arguments, ui_events):
    prompt = (arguments.get("prompt") or "").strip()
    if not prompt:
        return "错误：未提供图片描述（prompt）。"
    negative = (arguments.get("negative_prompt") or "").strip() or "low quality, blurry, watermark, text, deformed"
    size = int(arguments.get("size") or 512)
    # 提升 SD 对 prompt 的遵循度：追加质量词
    boosted = prompt + ", " + _IMAGE_PROMPT_BOOST
    t2i.unload()  # 确保显存空闲
    start = time.time()
    result = t2i.generate(boosted, negative_prompt=negative, steps=4,
                          width=size, height=size)
    cost = time.time() - start
    if not result.get("ok"):
        return f"图片生成失败：{result.get('error')}"
    # 把图片作为副作用发给前端展示；只把简短文本回给模型，避免占用上下文
    _ui(ui_events, {"type": "image", "mime": "image/png", "b64": result["b64"],
                    "prompt": prompt, "device": result.get("device"),
                    "model": result.get("model"), "cost_s": round(cost, 1)})
    return (f"已生成图片（{size}x{size}，{result.get('device')}，用 {round(cost,1)} 秒）。"
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
        return f"图片微改失败：{result.get('error')}"
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