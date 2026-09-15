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
import re
import sys
import time
import glob
import urllib.parse

from . import t2i
from . import file_tools
from . import memory as memory_mod
from . import doclib as library_mod
from . import docx_write


# =====================================================================
#  工具 Schema（发给模型）
# =====================================================================
def make_schemas(web_enabled: bool = False, kb_enabled: bool = False,
                 code_exec: bool = False, writing: bool = False) -> list:
    """返回工具 schema 列表。

    web_enabled=True 时才暴露联网搜索工具——保证"开关不开不联网"的约定：
    关着的时候模型连工具都看不到，自然不会去联网。
    kb_enabled 同理：关着就不给知识库工具。
    code_exec 同理：关着就不给"本地跑代码"的工具（默认关，避免模型擅自执行代码）。

    writing=True 是**长文创作**场景，只给最必要的几个工具（见下面）。
    """
    if writing:
        # 写作文/方案这类任务只需要「问细节」和「存文件」，
        # 其余工具（画图、搜图、文件系统、跑代码…）这轮根本用不上。
        # 砍掉它们的收益很实在：18 个工具的 schema ≈ 5800 token，
        # 而长文生成既要思考又要写几百上千字，额度本来就很紧张。
        picked = [_ASK_USER_SCHEMA, _LIBRARY_SCHEMA]
        if kb_enabled:
            picked.insert(0, _KB_SCHEMA)      # 写东西时查用户资料是常见需求
        return picked
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "web_image_search",
                "description": (
                    "【联网搜图】到网上找**已经存在**的真实图片并展示原图。\n"
                    "用户说「找/搜/看看……的图」「……长什么样」「来点……壁纸」时用它。\n"
                    "与 generate_image 的区别：本工具=找现成的真实图，不绘制；"
                    "generate_image=AI 从零画。说「画/生成/绘制」时用 generate_image。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "名词短语关键词，如「埃菲尔铁塔 照片」「橘猫 壁纸」。**只放名词**，不要塞「详细特征/介绍/怎么样」这类词，那样会搜出无关内容"},
                        "n": {"type": "integer", "description": "想要几张，默认 4，最多 8。用户嫌少时可以调大或再搜一次"},
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
                "description": "根据描述生成图片（文生图）。prompt 必须是详细具体的**英文**描述，生成后直接展示给用户。",
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
                "description": "对一张已有图片做局部微改（图生图），如「把背景改成夜晚」「戴上帽子」。source 填本地路径；若用户本轮拖入的图要微改则不填 source。prompt 用英文，并注明保持其他部分不变。",
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
                "description": "读取本机指定路径的文件（文本/代码/PDF）。仅当用户给出本地文件路径时用。图片/视频若已附在对话中，直接用视觉能力看，不要调用本工具。",
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
                "description": "写入或**修改**记忆。**长期记忆**跨所有对话共享（身份、姓名、职业、长期偏好、约定、目标计划）；**短期记忆**只在当前对话生效（正在做的项目、本次讨论的结论、临时设定）。寒暄、临时问答、一次性的提问不要存。系统会自动去重，不需要你重写旧内容。\n特别注意 action：add=新增（默认）；update=**改写已有条目**（目标有进展、计划变了、之前记错了，必须用这个而不是 add，否则档案里会留下互相矛盾的两句）；forget=作废删除（用户明确说不做了/弄错了）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "要记住的一条具体事实（第三人称、直接、可读，60 字以内）。action=update 时填**修改后**的新内容"},
                        "scope": {
                            "type": "string",
                            "enum": ["long", "short"],
                            "description": "long=长期记忆（跨对话通用，放身份/偏好/约定/目标计划）；short（默认）=短期记忆（只在这个对话用，放当前项目/本次结论）"},
                        "action": {
                            "type": "string",
                            "enum": ["add", "update", "forget"],
                            "description": "add（默认）=新增；update=改写已有条目；forget=删除已有条目"},
                        "old": {
                            "type": "string",
                            "description": "action=update/forget 时必填：要改动的那条记忆的**原句**（照抄，至少前 10 个字），用来定位是哪一条"},
                    },
                    "required": ["content"],
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
    # 知识库工具只在「知识库」开关打开时暴露 —— 与联网同样的约定：
    # 开关关着，模型连工具都看不到，自然就不会去翻资料。
    schemas.append(_LIBRARY_SCHEMA)
    schemas.append(_ASK_USER_SCHEMA)
    if kb_enabled:
        schemas.append(_KB_SCHEMA)
    if code_exec:
        schemas.append(_run_py_schema())
    if web_enabled:
        schemas.append(_WEATHER_SCHEMA)   # 天气走数据 API，比搜索可靠得多
        schemas.append(_WEB_SEARCH_SCHEMA)
    else:
        # ⚠️ 联网搜图走的是外网，必须跟着「联网」开关一起关。
        # 之前它写死在基础列表里，关着联网也能搜图 —— 与"关着不联网"的约定矛盾。
        schemas = [s for s in schemas
                   if s["function"]["name"] != "web_image_search"]
    return schemas


# ---------- 本地代码执行（"离线计算"）：受「本地算代码」开关控制 ----------
# 为什么要它：模型写代码不难，难的是**算对**。让它把代码真跑一遍，
# 数字、日期、正则匹配结果都是真算出来的，而不是"看着像"。
# 默认关闭 —— 打开后模型写的代码会在用户电脑上执行，风险由用户判断。
_SCI_LIBS = ("numpy", "scipy", "pandas", "matplotlib", "sympy")
_sci_cache = None


def available_science_libs() -> list:
    """当前环境里**真正能用**的科学计算库。

    用 find_spec 探测而不是真的 import —— 只为判断有没有，不值得拖慢启动。
    为什么要动态探测（实测）：源码环境和容器的库**不一样**，容器里就没有
    scipy / matplotlib。描述里写死库名会让模型写出跑不起来的代码，
    所以按本机实际能力告诉它。
    """
    global _sci_cache
    if _sci_cache is None:
        import importlib.util
        ok = []
        for m in _SCI_LIBS:
            try:
                if importlib.util.find_spec(m):
                    ok.append(m)
            except Exception:
                pass
        _sci_cache = ok
    return _sci_cache


def _run_py_schema() -> dict:
    libs = available_science_libs()
    libs_txt = "、".join(libs) if libs else "（本机没有额外的科学计算库，只能用标准库）"
    return {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "【本地执行 Python】在用户电脑上**真跑**一段 Python 代码并返回输出。\n"
                "什么时候用：需要精确计算、处理数据、验证自己写的算法对不对、"
                "做日期/单位换算、正则匹配测试等 —— 凡是「算出来比想出来更可靠」的场景都用它。\n"
                "怎么用：把完整可运行的代码放进去，用 print() 输出你要看的结果；\n"
                "**不要**用 input()（没人能输入），不要写文件以外的东西到磁盘（会在临时目录里执行）。\n"
                "可用库：标准库 + " + libs_txt + "。\n"
                "拿到输出后**依据真实结果**回答用户，不要把输出原样贴给用户就完事。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string",
                             "description": "完整可运行的 Python 代码，用 print() 打印要看的中间结果"},
                },
                "required": ["code"],
            },
        },
    }


# ---------- 生成文库（模型自己的产出物）----------
# 和知识库**严格分开**：知识库是用户的资料、只读；这里是模型产出、可读可写可删。
_LIBRARY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "library",
        "description": (
            "【生成文库】存放**你自己产出**的文件（作文 / 方案 / 报告 / 代码模块 / 表格等）。\n"
            "⚠️ 和「知识库」是两个不同的地方，别搞混：\n"
            "  · 知识库 = 用户的资料，**只能读、绝不能改**；\n"
            "  · 生成文库 = 你的产出，**可以写、可以改、可以删**。\n"
            "用 action 指定动作：\n"
            "· list —— 列出文库里的文件（不知道有什么就先列一下）\n"
            "· read —— 读文件正文（name）\n"
            "· write —— 写入/覆盖（name + content）；name 带 .md/.txt/.py/.json 等后缀\n"
            "· append —— 追加到文件末尾（name + content）\n"
            "· delete —— 删除（会移进回收站，可恢复）\n"
            "· copy —— 复制成新文件（name + new_name）\n"
            "· backup —— 整库备份\n"
            "· export_docx —— 把已写好的文本文件导出成 Word（name，会自动加 .docx 后缀）\n"
            "  Word 文档 WPS 也能直接打开，用户要「WPS 格式 / Word 文档」就用这个。\n"
            "什么时候写进文库：用户**明确要一个文件**时（「写成文档」「给我一个 Python 模块」"
            "「导出成 Word」「保存到文件」）。\n"
            "什么时候不写：只是让你「写篇作文 / 拟个方案」→ **直接写在回答里**就行，"
            "别自作主张建文件；不过长文写完可以问一句要不要存进文库。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "read", "write", "append", "delete",
                                    "copy", "backup", "export_docx"]},
                "name": {"type": "string",
                         "description": "文件名（可带子目录，如 作文/我的大学.md）。不要写盘符或 .."},
                "content": {"type": "string", "description": "write/append 时要写入的正文"},
                "new_name": {"type": "string", "description": "copy 时的目标文件名"},
            },
            "required": ["action"],
        },
    },
}


# ---------- 反问用户（材料不足时先把细节问清楚）----------
_ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "【向用户提问】在界面上弹出一个问答框，让用户补充信息，他填完你继续做。\n"
            "什么时候用：创作类任务缺关键信息时 —— 写作文/演讲稿/方案/总结/报告这类，"
            "如果用户没说清【用途、给谁看、字数、文体、要突出的重点、时间或背景】，"
            "**先问清楚再写**，比硬猜一篇强得多。\n"
            "⚠️ **必须调用这个工具来问，不要在回答里用文字提问** ——"
            "用户在弹框里填比在对话框里一条条回方便得多。\n"
            "什么时候别用：用户已经把要求说清楚了；能自己查到的客观事实"
            "（用 search_knowledge / web_search）；纯技术或计算任务。\n"
            "一次最多 4 个问题，每题给 2-4 个选项（用户也可以不选、自己写）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "要问的问题（1-4 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "要问的问题，一句话"},
                            "header": {"type": "string", "description": "短标签，4-8 个字"},
                            "options": {"type": "array", "items": {"type": "string"},
                                        "description": "2-4 个候选答案"},
                            "multi": {"type": "boolean", "description": "是否可多选"},
                        },
                        "required": ["question"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}


# 知识库检索工具（受「知识库」开关控制）
_KB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": (
            "检索本地知识库（用户导入的领域文档）。**这是用户自己的资料，"
            "优先级高于联网搜索**：涉及专业领域、内部规范、项目/产品资料时，先来这里查。\n"
            "· 建议先用 list_all=true 摸清有哪些文档，再针对性检索；\n"
            "· 一次没查到就换关键词再查，允许多轮检索；\n"
            "· 需要时效性信息时，可以**在同一轮里同时调用本工具和 web_search**，"
            "用知识库答内部细节、用网络补最新情况。\n"
            "（若系统提示里已出现「知识库资料」，说明自动检索已命中，不必重复查。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "检索关键词。用文档里可能出现的原词/术语效果最好"},
                "list_all": {"type": "boolean",
                             "description": "true = 列出全部文档的标题与开头（想先摸清有哪些资料时用）"},
            },
            "required": [],
        },
    },
}


# 天气工具：走结构化数据源，不要用搜索引擎
_WEATHER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "查询某地天气（实时 + 未来逐日预报）。"
            "**天气一律用这个，不要用 web_search**（搜索引擎只给天气网站导航页，没有数值）。"
            "支持中文城市名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，如「北京」「上海」，可带省份消歧：「广东 深圳」。",
                },
                "days": {
                    "type": "integer",
                    "description": "预报天数，默认 3（含今天），最多 16 天。",
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
            "联网搜索最新信息：新闻时事、近期事件、实时数据（股价/汇率/比分）、"
            "不确定或可能过时的内容、需要查证的事实。会真的联网检索并返回网页摘要。"
            "查天气请用 get_weather；查机构公开信息时关键词带上**机构全称**效果最好。"
            "闲聊、写作、翻译、代码等无需联网的任务不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "搜索关键词，2~4 个核心词，不要写成完整句子、不要塞年月日"
                        "（会被带偏成日历类结果）。中文提问用中文关键词。"
                        "限定官方来源可加 site:（如「深圳大学 招生章程 site:edu.cn」）。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "检索条数，默认 10（多引擎聚合）；想覆盖更广可调到 15~20",
                },
                "deep": {
                    "type": "integer",
                    "description": "对前 N 条结果抓取网页正文供精读，默认 4。"
                                   "需要更深入的细节（条款、数字、名单）时可调大到 6~8；"
                                   "只想快速了解概况可设 0。",
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
        return _do_remember(arguments, context)
    if name == "search_memory":
        return _do_search_memory(arguments)
    if name == "search_knowledge":
        return _do_search_knowledge(arguments)
    if name == "run_python":
        return _do_run_python(arguments, ui_events, context)
    if name == "library":
        return _do_library(arguments, ui_events)
    if name == "ask_user":
        return _do_ask_user(arguments, context)
    if name == "get_time":
        return time.strftime("%Y-%m-%d %H:%M:%S (%A)")
    if name == "web_search":
        return _do_web_search(arguments, ui_events)
    return f"[未知工具] {name}"


# ---------- 联网搜索 ----------
# 查询里常见的"干扰词"：搜索引擎对这类限定词很敏感，会大幅降低召回质量
_NOISE_PATTERNS = [
    r"是公办还是民办", r"公办还是民办", r"公办\s*民办", r"是公办的吗", r"是民办的吗",
    r"是什么", r"怎么样", r"怎样", r"怎么", r"有哪些", r"是多少", r"为什么",
    r"怎么回事", r"如何",
    r"的参数", r"参数配置", r"规格", r"详细介绍", r"介绍一下", r"请问",
]

# 用户口语化的"意图尾巴"：出现在句末时几乎没有检索价值，却会把引擎带偏。
# 例如「2026年人工智能技术应用专业的就业前景如何？请详细分析」——
# 不剥掉后半句时，引擎会去匹配"2026年…分析"，返回国务院节假日通知之类的垃圾；
# 只留「人工智能技术应用专业的就业前景」，召回质量立刻正常。
_TRAILING_INTENT = (
    "请详细分析", "详细分析一下", "详细分析", "分析一下", "帮我分析",
    "请详细说明", "详细说明一下", "详细说明", "说明一下",
    "请详细介绍一下", "详细介绍一下", "请介绍一下", "介绍一下",
    "解释一下", "讲讲", "说说", "谈谈", "聊一聊", "告诉我",
    "帮我看看", "帮我看一下", "帮我查查", "帮我查一下", "我想知道", "想知道",
    "怎么做", "怎么办", "要注意什么", "有什么建议",
)


# 意图从句的起始词：形如「并给出你的分析」「，请详细说明」这类尾巴要整段丢掉
_INTENT_HEAD = (
    "请", "帮我", "麻烦", "分析", "说明", "解释", "给出", "提供", "介绍",
    "讲讲", "说说", "谈谈", "告诉我", "指出", "评价", "对比", "推荐",
)
_TRAILING_CLAUSE_SEP = r"[，,、；;。.]+|以及|并且|还有|并|和"


def _strip_trailing_intent(s: str) -> str:
    """反复剥掉句末的口语化意图短语 / 意图从句。

    例：'深圳大学2026年招生有什么新变化？请详细说明并给出你的分析'
        → 先按并列词切掉「并给出你的分析」→ 再剥掉「请详细说明」
        → '深圳大学 招生有什么新变化'
    """
    import re
    changed = True
    while changed:
        changed = False
        t = (s or "").strip(" 　,，。.、；;：:！!？?")
        # 1) 句末的并列意图从句（「并给出你的分析」这种）
        parts = re.split("(" + _TRAILING_CLAUSE_SEP + ")", t)
        while len(parts) >= 3:
            last = parts[-1].strip()
            if last and last.startswith(_INTENT_HEAD):
                parts = parts[:-2]
                changed = True
            else:
                break
        t = "".join(parts).strip()
        # 2) 句末的固定意图短语
        for p in _TRAILING_INTENT:
            if t.endswith(p):
                t = t[: -len(p)].strip(" 　,，。.、；;：:！!？?")
                changed = True
        # 3) 句末的孤立问句助词
        t2 = re.sub(r"[吗呢吧啊呀嘛]$", "", t).strip()
        if t2 != t:
            t, changed = t2, True
        # 4) 空格分隔的孤立意图词：如「…就业前景 分析」。
        #    要求**前面有空格**才剥，否则会误伤「数据分析」这类连写词。
        m = re.match(r"^(.*\s)(\S{2,4})$", t)
        if m and m.group(2) in _INTENT_HEAD:
            t, changed = m.group(1).strip(), True
        s = t
    return s


def _simplify_query(q: str) -> str:
    """去掉年份/月日/疑问词/口语尾巴，得到更通用的检索词。

    实测：搜索引擎对"广州民航职业技术学院 公办 民办"这类长查询召回很差，
    而只留主体词"广州民航职业技术学院"时能正常返回官网与百科。
    """
    import re
    s = _strip_trailing_intent(q)
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
    """联网检索：多引擎 + 多查询变体聚合 + 相关性过滤 + 深度阅读正文。

    深度阅读（deep read）是关键一步：只给模型 140 字的搜索摘要，它写不出
    有内容的回答；把前几条结果的**正文**抓下来喂给它，才有材料"总结 + 分析 + 展开"。
    """
    from concurrent.futures import ThreadPoolExecutor

    query = (arguments.get("query") or "").strip()
    if not query:
        return "联网搜索失败：未提供 query。"
    try:
        top_k = int(arguments.get("top_k") or 10)
    except Exception:
        top_k = 10
    top_k = max(1, min(top_k, 20))

    # 深度阅读前 N 条正文；正文字数上限
    # 注意：这些量直接决定注入提示词的体积。单条正文每条几百 token，
    # 抓太多会把上下文挤爆（也拖慢推理），因此克制：
    #   4 篇 × 800 字 ≈ 2400 token，加上 8 条标题/链接/摘要 ≈ 1200 token，
    #   合计约 3600 token —— 与主流程 _trim_history_to_budget 里的预留量对齐。
    deep_n = max(0, min(int(arguments.get("deep") if arguments.get("deep") is not None else 4), 8))
    deep_chars = 800

    from . import web_tools

    # 查询变体：原查询 + 精简主体词（+ 去数字版 + 机构类查询追加"官方站定向"）
    variants = [query]
    simple = _simplify_query(query)
    if simple and simple != query and len(simple) >= 2:
        variants.append(simple)

    # 变体 3：把残留的数字也去掉。
    # 「2026 人工智能技术应用 就业前景」这类查询里的年份会把引擎引向
    # 「2026年政府工作报告」「2026年节假日安排」等政策新闻，而"去数字"版本
    # 往往能召回真正讲专业前景的文章。实测对结果数量提升明显。
    import re as _re
    if _re.search(r"\d", simple or query):
        nodigit = _re.sub(r"\d+\s*[年月日]?", " ", simple or query)
        nodigit = _re.sub(r"\s+", " ", nodigit).strip()
        if len(nodigit) >= 2 and nodigit not in variants:
            variants.append(nodigit)

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
    # keep_min 给到 6：材料太少时模型写不出有内容的回答。宁可多留几条让模型自己取舍
    # —— 它已被明确告知"材料弱就如实说明"，比只给 3 条更好。
    filtered = []
    for (q, kind), b in zip(jobs, batches):
        filtered.append(web_tools.filter_relevant(
            q, b, keep_min=4 if kind == "official" else 6))

    # 组装优先级：原查询 → 官方站定向 → 精简查询
    order = [0] + [i for i, (_, k) in enumerate(jobs) if k == "official"] \
        + [i for i in range(1, len(jobs)) if jobs[i][1] != "official"]
    results, seen_idx = [], set()
    for i in order:
        if i in seen_idx or i >= len(filtered):
            continue
        seen_idx.add(i)
        results = _dedupe(results + filtered[i])
    # 扩大检索范围：不再只留 8 条，多给些材料让模型有得比、有得选
    cap = max(top_k, 12)
    results = results[:cap]

    # 相关度偏低时，明确告诉模型"这次检索不可靠"，避免它硬编内容
    confidence = web_tools.best_relevance(query, results)

    used = f"{query}（含精简检索：{simple}）" if len(variants) > 1 else query
    if official_hint:
        used += f"；已定向官方站 site:{official_hint}"

    # ---------- 深度阅读：抓前 N 条的正文 ----------
    # 摘要只有一两百字，模型据此只能写得很短。抓正文才能"总结 + 分析 + 展开"。
    pages = {}
    if deep_n and results:
        urls = [r.get("url") for r in results[:deep_n] if r.get("url")]
        try:
            pages = web_tools.fetch_pages(urls, limit=deep_chars, workers=5, timeout=8)
        except Exception:
            pages = {}

    # ---------- 把来源清单作为独立事件发给前端（聊天气泡里不再重复列链接）----------
    src_items = []
    for i, r in enumerate(results, 1):
        url = (r.get("url") or "").strip()
        try:
            site = urllib.parse.urlparse(url).netloc
        except Exception:
            site = ""
        src_items.append({
            "i": i,
            "title": (r.get("title") or "").strip(),
            "url": url,
            "site": site,
            "engine": r.get("engine") or "",
            "read": url in pages,      # 是否已深度阅读（前端可标注）
        })
    _ui(ui_events, {"type": "sources", "query": used, "items": src_items})

    # 输出给模型的材料：标题 + 完整链接 + 搜索摘要 + 正文节选
    lines = [f"【联网检索结果】检索词：{used}　共 {len(results)} 条，"
             f"其中 {len(pages)} 条已抓取网页正文供你精读。"]
    useful = 0
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("desc") or "").strip()
        if title and title not in ("搜索失败",) and "未获取到搜索结果" not in title and url:
            useful += 1
        lines.append(f"[{i}] {title}")
        if url:
            lines.append(f"    链接：{url}")
        if desc:
            lines.append(f"    摘要：{desc[:200]}")
        body = pages.get(url)
        if body:
            lines.append(f"    正文：{body}")
    if useful == 0:
        lines.append("（未获取到有效搜索结果。请如实告诉用户本次联网检索失败，不要编造内容；"
                     "可以建议用户换个更具体的说法再试。）")
    else:
        if confidence < 0.3:
            lines.append("（注意：本次检索结果与问题的匹配度较低，可能没有命中要点。"
                         "请如实说明「未检索到直接相关信息」，并建议用户换个关键词或提供更具体的名称，"
                         "不要用这些弱相关结果硬凑答案。）")
        lines.append(
            "请基于以上检索结果，用中文写一份**充实、有分析的回答**。要求：\n"
            "1. **篇幅要够**：不要只写两三句结论。一般 400~900 字，信息多的可更长。\n"
            "2. **结构清晰**：用「小标题 + 分点」组织，便于阅读；\n"
            "3. **先总结、再分析、后展开**：\n"
            "   · 先用一两句给出核心结论；\n"
            "   · 再把各条材料的**具体信息**（名称、数字、时间、条款、名单等）提炼出来，\n"
            "     不要笼统概括，要落到细节；\n"
            "   · 有「正文」字段的条目是已抓取的网页正文，**优先从中提取细节**；\n"
            "   · 不同来源说法不一致时，指出来并说明差异所在；\n"
            "4. **给出你自己的判断**：单独一小段「几点看法」或类似小标题，\n"
            "   基于材料做推断和评价（如适用性、风险、值得注意之处）。\n"
            "   **属于你的推断要明确说是推断**，不要和检索到的事实混为一谈；\n"
            "5. **标注来源**：引用了哪条材料，就在该句末尾用 [序号] 标注（如「……[2]」）；\n"
            "6. **不要再写「信息来源」「参考资料」这类列表** —— "
            "界面已经在回答下方单独提供了可展开的来源清单，重复列出是冗余；\n"
            "7. 只能引用上面真实出现过的条目，**绝不编造链接、数字或事实**。\n"
            "   特别注意：**不要编造电话号码、地址、邮箱、文号、日期等联系方式或标识**。\n"
            "   材料里没有就别写；更不要用「020-XXXXXXX」这种占位式写法凑数——\n"
            "   要提供联系方式就写「建议从官网获取」，否则会误导用户；\n"
            "8. 若材料确实不足以回答某部分，就如实说明「检索结果未涉及」，不要凭空补充。\n"
            "注意：当前时间以系统提示中的时间为准；涉及时效的信息请说明材料日期。")
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
    n = max(1, min(n, 8))

    # 多要一些候选再筛：搜索结果里总有一部分因为**防盗链或链接失效**下载不下来，
    # 只按 n 条去取的话，实际能展示的会明显少于预期 —— 用户反馈过"搜图图片太少"。
    try:
        results = web_tools.image_search(query, n=max(n * 3, 12))
    except Exception as exc:
        return f"联网搜图失败：{exc}"
    if not results:
        return (f"联网搜图没有找到「{query}」的图片。"
                "请**如实**告诉用户没搜到，不要凭印象去描述图片长什么样。")

    shown, tried, lines = 0, 0, [f"【联网搜图】关键词：{query}"]
    for r in results:
        if shown >= n:
            break
        tried += 1
        referer = r.get("source") or None
        raw = web_tools.download_image(r["url"], referer=referer)
        if not raw and r.get("thumb"):
            # 原图被防盗链挡住时退一步用缩略图 —— 缩略图通常挂在允许外链的 CDN 上，
            # 虽然小一些，但总比"一张都显示不出来"强。
            raw = web_tools.download_image(r["thumb"], referer=referer)
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
        return (f"联网搜图失败：找到 {len(results)} 个候选，但图片都没能下载下来"
                "（多为目标站点的防盗链）。请**如实**说明没取到图片，"
                "不要改口去描述图片内容，也不要编造物种特征。")

    lines.append(
        f"已把 {shown} 张**网上搜索到的真实原图**展示给用户（这是搜索结果，不是你画的）。"
        f"（本轮共检查 {tried} 个候选，其余因防盗链或链接失效未能取到。）"
        "请用中文简要说明找到了什么内容，并提示：可点图片下方「保存到图库」留存。"
        "**只描述实际看到的内容，不要补充未经核实的事实**（搜图搜不到生物学特征）。")
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
def _do_remember(arguments, context=None):
    """写入 / 改写 / 删除记忆。

    scope=long 动长期记忆（跨对话共享），否则动当前对话的短期记忆。
    短期记忆按对话隔离，所以必须从 context 拿 session —— 拿不到就拒绝，
    免得内容写进了不知道哪个对话（等于丢失）。

    action 三种：
      add（默认）—— 新增一条
      update      —— 把已有条目改成新内容（**目标的进展就靠它**）
      forget      —— 删掉已有条目
    update/forget 必须给 old（原句），用来定位是哪一条；
    old 可以少写几个字（匹配是模糊的，见 memory._locate）。
    """
    content = (arguments.get("content") or arguments.get("section") or "").strip()
    action = (arguments.get("action") or "add").strip().lower()
    old = (arguments.get("old") or "").strip()
    scope = (arguments.get("scope") or "").strip().lower()
    if action in ("update", "forget") and not old:
        # 没给原句就没法定位 —— 直接把新内容当新条目存，总比丢掉强
        action, old = "add", ""
    if not content and action != "forget":
        return "错误：内容为空。"
    is_long = scope in ("long", "global")

    if is_long:
        if action == "update":
            r = memory_mod.update_long(old, content)
            return {"replaced": "已更新长期记忆里的那条。",
                    "added": "长期记忆里没找到那条，已作为新内容记下。",
                    "skipped": "内容和原来一样，没有改动。"}.get(r, "已处理。")
        if action == "forget":
            return ("已从长期记忆里删除那条。" if memory_mod.drop_long(old)
                    else "长期记忆里没找到那条，未改动。")
        ok = memory_mod.merge_long(content)
        return ("已记入长期记忆（所有对话都通用）。" if ok
                else "这条已经在长期记忆里了，没有重复记。")

    session = str(((context or {}).get("session")) or "").strip()
    if not session:
        return "错误：无法确定当前对话，记忆未写入。"
    if action == "update":
        r = memory_mod.update_short(session, old, content)
        return {"replaced": "已更新本对话记忆里的那条。",
                "added": "本对话记忆里没找到那条，已作为新内容记下。",
                "skipped": "内容和原来一样，没有改动。"}.get(r, "已处理。")
    if action == "forget":
        return ("已从本对话记忆里删除那条。" if memory_mod.drop_short(session, old)
                else "本对话记忆里没找到那条，未改动。")
    ok = memory_mod.merge_short(session, content)
    return ("已记入本次对话的短期记忆。" if ok else "这条已经在本对话记忆里了，没有重复记。")


# ---------- 本地代码执行（"离线计算"）----------
RUN_TIMEOUT = 25          # 秒。计算题够用，也避免死循环把机器占住

# 需要"先问用户"的操作。**不再一律拦截** ——
# 用户明确要求：发现危险操作先问一句，批准了就执行，而不是直接拒绝。
# 每项配一句人话理由，前端弹窗直接展示给用户看。
# 说明：这不是滴水不漏的沙箱（真正的沙箱要上容器/权限隔离），
# 而是一道"明显危险就先问一句"的闸门；开关默认关闭，风险由用户自己权衡。
_PY_RISKY = (
    (("shutil.rmtree", "os.removedirs", "os.rmdir", "os.remove", "os.unlink",
      "del /f", "del /s", "rmdir /s", "rm -rf", "format c:", "format d:"), "会删除文件或目录"),
    (("subprocess", "os.system", "os.popen", "os.exec", "os.startfile",
      "os.spawn", "os.fork"), "会启动外部程序"),
    (("winreg", "reg delete", "reg add"), "会修改 Windows 注册表"),
    (("shutdown", "reboot"), "可能影响系统开关机"),
    (("socket", "urllib", "requests", "httpx", "urlopen", "ftplib", "smtplib"),
     "会联网（这个模块本来是给'离线计算'用的）"),
    (("ctypes",), "会直接调用系统底层接口"),
    (("eval(", "exec("), "会动态执行字符串代码"),
)


def scan_risky(code: str) -> list:
    """返回代码里命中的风险项（人话描述）。空列表 = 没发现风险。

    注释行先剔掉 —— 免得"注释里提了一句 subprocess"也被当成风险。
    """
    scanned = "\n".join(ln for ln in (code or "").splitlines()
                         if not ln.strip().startswith("#"))
    low = scanned.lower()
    hits = []
    for pats, label in _PY_RISKY:
        if any(p in low for p in pats):
            hits.append(label)
    return hits


def run_code(code: str, allow_risky: bool = False) -> dict:
    """执行一段 Python，返回**结构化**结果（工具与前端接口共用）。

    字段：needs_confirm / risky / out / err / rc / seconds
    needs_confirm=True 表示"检测到风险但还没获批准"，**没有执行**。
    """
    code = str(code or "").strip()
    if not code:
        return {"needs_confirm": False, "risky": [], "out": "", "err": "代码为空",
                "rc": -1, "seconds": 0}
    risky = scan_risky(code)
    if risky and not allow_risky:
        return {"needs_confirm": True, "risky": risky, "out": "", "err": "",
                "rc": None, "seconds": 0}
    import tempfile
    import subprocess as _sp
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="mm_run_") as d:
        path = os.path.join(d, "snippet.py")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as exc:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                    "seconds": 0, "err": "无法写入临时文件：%s" % exc}
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 不让被执行的代码摸到应用的数据目录
        env.pop("MM_DATA_DIR", None)
        try:
            p = _sp.run([sys.executable, "-X", "utf8", "snippet.py"],
                        cwd=d, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            rc = p.returncode
        except _sp.TimeoutExpired:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": None,
                    "seconds": round(time.time() - t0, 2),
                    "err": "执行超过 %d 秒，已被强制中止。" % RUN_TIMEOUT}
        except Exception as exc:
            return {"needs_confirm": False, "risky": risky, "out": "", "rc": -1,
                    "seconds": round(time.time() - t0, 2),
                    "err": "%s: %s" % (type(exc).__name__, exc)}
    return {"needs_confirm": False, "risky": risky, "out": out, "err": err,
            "rc": rc, "seconds": round(time.time() - t0, 2)}


def _format_py_result(r: dict) -> str:
    """把执行结果整理成给模型看的文本。"""
    lines = ["【代码执行结果】"]
    out = r.get("out") or ""
    err = r.get("err") or ""
    if out:
        if len(out) > 4000:
            out = "（输出过长，只保留最后 4000 字）\n" + out[-4000:]
        lines.append("标准输出：\n" + out)
    if err:
        head = "运行提示：\n" if r.get("rc") in (0, None) else "报错信息：\n"
        lines.append(head + err[-1500:])
    if not out and not err:
        lines.append("（代码没有输出任何内容 —— 别忘了用 print() 把结果打出来）")
    if r.get("rc") not in (0, None):
        lines.append("（退出码 %s，说明代码报错了；请先修正再给出结论）" % r.get("rc"))
    if r.get("risky"):
        lines.append("（这次执行包含用户已批准的操作：%s）" % "、".join(r["risky"]))
    lines.append("请**依据上面的真实输出**回答用户；如果代码报错，先说清错在哪并给出修正后的代码。")
    return "\n\n".join(lines)


def _do_run_python(arguments, ui_events=None, context=None):
    """在本机真跑一段 Python，把 stdout 拿回来。

    为什么要真跑：模型"心算"很容易出错（数字、日期、正则尤其明显），
    而代码跑一遍的结果是**确定的**。这也是"离线计算"的落点。

    ⚠️ 检测到危险操作时**不是直接拒绝，而是先问用户**（用户明确要求）：
    批准了就执行，拒绝才放弃。没有确认通道时（例如脚本里直接调用）默认**不执行**。
    """
    code = str((arguments or {}).get("code") or "").strip()
    r = run_code(code, allow_risky=False)
    if r.get("needs_confirm"):
        ask = (context or {}).get("confirm")
        risk = "、".join(r.get("risky") or [])
        if not callable(ask):
            return ("这段代码里有需要用户确认的操作（%s），但当前没有可用的确认通道，**没有执行**。\n"
                    "请改成不涉及这些操作的写法。" % risk)
        allowed = ask({"kind": "python", "code": code,
                       "risky": r.get("risky") or [],
                       "reason": "检测到：" + risk})
        if not allowed:
            return ("用户**拒绝了**这次执行（原因：%s）。\n"
                    "请换一种不涉及这些操作的写法；如果确实必须这么做，"
                    "先把你要做什么、为什么这么做说清楚，等用户同意再试。\n"
                    "⚠️ **绝对不要编造执行结果或用模拟数据冒充真实输出** ——"
                    "那会让用户以为结果是真的。如实说明「被拒绝了」即可。" % risk)
        r = run_code(code, allow_risky=True)
    # 把这次执行的代码与结果推给前端 → 界面渲染成"可直接编辑重跑"的代码卡片
    if isinstance(ui_events, list):
        ui_events.append({"type": "code", "code": code,
                          "out": r.get("out") or "", "err": r.get("err") or "",
                          "rc": r.get("rc"), "seconds": r.get("seconds"),
                          "risky": r.get("risky") or []})
    return _format_py_result(r)


# =====================================================================
#  生成文库工具
# =====================================================================
def _do_library(arguments, ui_events=None):
    """生成文库的增删改查 + 导出 Word。"""
    action = str((arguments or {}).get("action") or "list").strip().lower()
    name = str((arguments or {}).get("name") or "").strip()
    content = (arguments or {}).get("content")
    new_name = str((arguments or {}).get("new_name") or "").strip()
    content = "" if content is None else str(content)

    def _notify(kind, **kw):
        if isinstance(ui_events, list):
            ui_events.append(dict(type="library", act=kind, **kw))

    try:
        if action == "list":
            files = library_mod.list_files()
            if not files:
                return "生成文库现在是空的。要写文件的话用 action=write 并给 name 和 content。"
            lines = ["【生成文库】共 %d 个文件：" % len(files)]
            for f in files[:60]:
                lines.append("- %s（%s，%d 字节）" % (f["rel"], f["modified"], f["size"]))
            return "\n".join(lines)

        if action == "read":
            r = library_mod.read_file(name)
            if not r.get("ok"):
                return "读取失败：%s" % r.get("error")
            _notify("read", rel=r["rel"], content=r.get("text") or "")
            head = r.get("text") or ""
            if r.get("chars", 0) > 20000:
                head = head[:20000] + "\n……（内容很长，已截断）"
            return "【%s】共 %d 字：\n\n%s" % (r["rel"], r.get("chars", 0), head)

        if action in ("write", "append"):
            r = library_mod.write_file(name, content,
                                       "append" if action == "append" else "overwrite")
            if not r.get("ok"):
                return "写入失败：%s" % r.get("error")
            _notify("write", rel=r["rel"], chars=r.get("chars", 0))
            tip = "（原有内容已自动备份）" if r.get("backup") else ""
            return ("已%s到生成文库：%s（%d 字）%s。\n"
                    "告诉用户文件已经存好了、存在生成文库面板里，他可以在那里打开、编辑、"
                    "导出 Word 或删除。" % ("追加" if action == "append" else "写入",
                                            r["rel"], r.get("chars", 0), tip))

        if action == "delete":
            r = library_mod.delete_file(name)
            if not r.get("ok"):
                return "删除失败：%s" % r.get("error")
            _notify("delete", rel=r["rel"])
            return "已删除 %s（实际移到了回收站 _回收站/，需要的话可以恢复）。" % r["rel"]

        if action == "copy":
            r = library_mod.copy_file(name, new_name)
            if not r.get("ok"):
                return "复制失败：%s" % r.get("error")
            _notify("write", rel=r["to"], chars=0)
            return "已复制：%s → %s" % (r["from"], r["to"])

        if action == "backup":
            r = library_mod.backup_all()
            if not r.get("ok"):
                return "备份失败：%s" % r.get("error")
            return "已备份 %d 个文件到 %s" % (r.get("count", 0), r.get("path", ""))

        if action == "export_docx":
            rd = library_mod.read_file(name)
            if not rd.get("ok"):
                return "导出失败：%s" % rd.get("error")
            base = rd["rel"]
            out = re.sub(r"\.(md|markdown|txt|text)$", "", base, flags=re.I) + ".docx"
            if out == base:
                out = base + ".docx"
            data = docx_write.text_to_docx(rd.get("text") or "",
                                           title=os.path.splitext(os.path.basename(base))[0])
            w = library_mod.save_bytes(out, data)
            if not w.get("ok"):
                return "导出失败：%s" % w.get("error")
            _notify("write", rel=w["rel"], chars=0)
            return ("已导出 Word 文档：%s（%d 字节）。WPS 和 Word 都能直接双击打开。\n"
                    "告诉用户去生成文库面板下载/打开它。" % (w["rel"], w.get("bytes", 0)))

        return "未知的 action：%s（可用 list/read/write/append/delete/copy/backup/export_docx）" % action
    except Exception as e:
        return "文库操作失败：%s: %s" % (type(e).__name__, e)


# =====================================================================
#  反问用户
# =====================================================================
def _do_ask_user(arguments, context=None):
    """把问题弹到界面上，等用户回答。复用"确认弹窗"那条通道。"""
    qs = (arguments or {}).get("questions") or []
    if not isinstance(qs, list) or not qs:
        return "错误：没有可问的问题。"
    norm = []
    for q in qs[:4]:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:4]
        norm.append({"question": text,
                     "header": str(q.get("header") or "").strip()[:12],
                     "options": opts, "multi": bool(q.get("multi"))})
    if not norm:
        return "错误：问题的格式不对，至少要有一个非空 question。"
    ask = (context or {}).get("ask")
    if not callable(ask):
        return ("现在没有可用的提问通道（用户界面没连上），先把最合理的默认方案做出来，"
                "并在回答里说明你假设了什么、哪些地方需要他补充。")
    answers = ask({"questions": norm})
    if not answers:
        return ("用户没有回答（可能直接关掉了弹框）。请**按最合理的默认假设继续做**，"
                "并在回答开头明确写出你替他假设了哪些条件，方便他纠正。")
    lines = ["【用户补充的信息】"]
    for i, a in enumerate(answers, 1):
        if isinstance(a, dict):
            lines.append("%d. %s → %s" % (i, a.get("question", ""), a.get("answer", "")))
        else:
            lines.append("%d. %s" % (i, a))
    lines.append("请**基于这些信息**继续完成，不要再重复问同样的问题。")
    return "\n".join(lines)


def _do_search_knowledge(arguments):
    """检索本地知识库。让模型**自己决定**要不要查、查什么。

    以前只有"每轮自动注入前 top_k 篇"，模型没法在需要时多查几轮；
    现在给它这个工具，它可以：先列目录看有哪些资料 → 再按关键词精查 →
    必要时换关键词再查一遍，甚至与联网搜索同时使用。
    """
    from . import kb as kb_mod
    docs = kb_mod.list_documents()
    if not docs:
        return ("知识库是空的。请告诉用户：把 .txt/.md 文档放进知识库文件夹即可，"
                "之后就能自动检索。")

    # 先摸清有哪些资料
    if arguments.get("list_all"):
        lines = [f"知识库共有 {len(docs)} 篇文档："]
        for i, d in enumerate(docs, 1):
            fn = d.get("filename") or d.get("id") or "?"
            # 顺带把开头一小段带出来，模型才好判断"这篇是不是我要的"
            try:
                with open(os.path.join(kb_mod.KB_DIR, fn), "r", encoding="utf-8") as f:
                    head = f.read(90).replace("\n", " ").strip()
            except Exception:
                head = ""
            lines.append(f"{i}. 《{fn}》（{d.get('chars', 0)} 字）{head}")
        lines.append("\n需要细节时，用 query 参数针对性检索。")
        return "\n".join(lines)

    query = (arguments.get("query") or "").strip()
    if not query:
        return ("请给出 query 参数（检索关键词），或用 list_all=true 先列出知识库有哪些文档。")

    hits = kb_mod.search(query, top_k=int(arguments.get("top_k") or 6))
    if not hits:
        return (f"知识库里没有与「{query}」相关的内容。"
                f"（当前共 {len(docs)} 篇文档；可换关键词再试，或 list_all=true 看看都有什么）")
    lines = [f"知识库检索结果（关键词：{query}）："]
    for i, h in enumerate(hits, 1):
        title = h.get("filename") or h.get("doc_id") or "?"
        body = (h.get("content") or "").strip()
        lines.append(f"\n[{i}] 《{title}》\n{body[:1200]}")
    lines.append("\n（以上来自用户自己的知识库，比联网结果更贴合其领域；"
                 "回答时请优先采用，并注明出自哪一篇。）")
    return "\n".join(lines)


def _do_search_memory(arguments):
    query = arguments.get("query") or ""
    hits = memory_mod.search_all(query, top_k=5)
    if not hits:
        return "没有找到相关记忆信息。"
    lines = ["检索到的历史记忆："]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h.get('level','?')}]{h.get('content','')}")
    return "\n".join(lines)