# -*- coding: utf-8 -*-
"""本地多模态助手 —— FastAPI 后端服务
用法:
    py -3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或直接:
    py -3 run.py
"""
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import os
import sys
import base64
import io
import json
import time
import asyncio
import datetime
import threading
from . import (config, ollama_client, memory, kb, file_tools, video, web_tools,
               t2i, tools, voice, sessions, image_library)

app = FastAPI(title="本地多模态助手", version="1.0.0")
client = ollama_client.OllamaClient()

# 最近一次出现过的图片（base64），供后续「把这张图改成…」直接微改，免去重新导入。
# 单用户桌面应用，保存最近一张即可；换新图时自动覆盖。
_LAST_IMAGE: dict = {"b64": None, "ts": 0.0, "ttl": 3600.0}


def _remember_image(images: list) -> None:
    """记住本轮图片，供下一轮引用。"""
    if images:
        _LAST_IMAGE["b64"] = images[-1]
        _LAST_IMAGE["ts"] = time.time()


def _recent_image() -> list:
    """取回最近一张仍在有效期内的图片（列表形式，未过期才返回）。"""
    b64 = _LAST_IMAGE.get("b64")
    if b64 and (time.time() - _LAST_IMAGE.get("ts", 0)) < _LAST_IMAGE.get("ttl", 3600):
        return [b64]
    return []


# 启动时做一次轻量清理：移除「太久未用 + 几乎没内容」的僵尸会话。
# 有实际内容的会话一律保留；真正重要的信息由长期记忆承载，不靠聊天记录堆积。
try:
    _cleaned_sessions = sessions.cleanup_old()
except Exception:
    _cleaned_sessions = 0


def _now_str() -> str:
    """返回本地当前时间的中文描述，供注入系统提示，让模型具备时间感知。"""
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日（星期{wd}），{now.strftime('%H:%M:%S')}"


# 简单问题识别：命中则收紧生成长度，避免模型对"你好"这类问题长篇思考。
# qwen3-vl 的 thinking 无法通过 API 关闭（think=false /no_think 均实测无效，
# 提示词引导反而让它想更多），**限制 num_predict 是唯一有效手段**：
# 实测「你好」从 24.3s 降到 3.9s。
_COMPLEX_HINTS = (
    "写", "画", "生成", "做一份", "方案", "报告", "代码", "脚本", "文件", "搜索",
    "查一下", "查查", "总结", "翻译", "分析", "对比", "设计", "规划", "计划",
    "记住", "帮我", "为什么", "怎么", "如何", "详细", "解释", "列出", "整理",
    # 图片相关一律走完整模式：微改需要输出工具调用，收紧长度会被思考吃光
    "图", "照片", "图片", "这张", "那张", "刚才", "上面", "改成", "换成", "修改",
)


def _is_simple_question(text: str) -> bool:
    """判断是否属于可快速作答的简单问题。

    注意：只做**文本**层面的判断；调用方还需确认本轮没有图片上下文
    （见 chat()），否则模型可能来不及输出工具调用。
    """
    t = (text or "").strip()
    if not t or len(t) > 30:
        return False
    return not any(k in t for k in _COMPLEX_HINTS)


# 简单问题的生成长度上限（思考+回答总量）。512 足以覆盖问候/常识问答，
# 又能把"先想很久"压到 3~5 秒；复杂问题仍用配置里的完整配额。
SIMPLE_MAX_TOKENS = 512

# 联网场景的生成长度下限：要把搜索结果喂给模型 + 让它逐条列出来源链接，
# token 消耗远高于普通问答。给少了就会出现"搜索完了但没输出回答"。
WEB_MAX_TOKENS = 4096

# 送入模型的历史消息上限（约 20 轮）。更早的内容已保存在会话文件与长期记忆中，
# 无需全部塞进上下文——否则推理越来越慢，且容易撑爆窗口。
MAX_CONTEXT_MESSAGES = 40

# 触发自动记忆的信号词：出现这些词说明用户可能透露了值得长期记住的信息。
# 只在命中时才做后台提炼，避免每轮都多跑一次模型。
_MEMORY_SIGNALS = (
    "记住", "我叫", "我是", "我的", "我喜欢", "我习惯", "我在", "我们公司",
    "以后", "下次", "偏好", "别忘", "提醒我", "我的名字", "叫我",
)


def _looks_memorable(text: str) -> bool:
    return any(k in (text or "") for k in _MEMORY_SIGNALS)


def _auto_extract_memory(last_user: str, answer: str, model: str, cfg: dict) -> None:
    """把本轮要点提炼进长期记忆（后台线程，不阻塞回复）。

    长期记忆是"久远对话可被清理"的前提——要点沉淀下来后，
    老的聊天记录就可以安全裁剪或清除。
    """
    try:
        prompt = (
            "从下面这轮对话里提取值得【长期记住】的用户信息（身份、背景、偏好、约定）。\n"
            "若有，只输出一段中文要点（不超过 30 字，不要任何前后缀）；\n"
            "若没有值得长期记住的内容，只输出两个字：无\n\n"
            f"用户：{last_user}\n助手：{(answer or '')[:300]}"
        )
        resp = client.chat([{"role": "user", "content": prompt}], model=model,
                           stream=False,
                           params={"temperature": 0.2, "num_ctx": 4096, "max_tokens": 120})
        data = resp.json() if hasattr(resp, "json") else resp
        text = ((data.get("message") or {}).get("content") or "").strip()
        text = text.split("\n")[0].strip()
        if not text or text.startswith("无") or len(text) > 60:
            return
        # 并入「当前关注」分区（文段式整体维护）
        memory.upsert("当前关注", text)
    except Exception:
        pass


# 允许本地界面跨域访问（浏览器 debug 时用）
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# 前端静态目录（打包后为只读打包资源）
FRONTEND_DIR = config.res("frontend")


# ---------- 数据模型 ----------
class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    images_b64: list[str] | None = None   # 附加到本轮 user 消息的图片
    stream: bool = True
    session_id: str | None = None         # 会话标识，用于历史会话透视归档


# ---------- 图片保存 ----------
class SaveImageRequest(BaseModel):
    b64: str | None = None                # 图片 base64 正文（无 data: 前缀）
    filename: str | None = None           # 期望文件名（自动去重）
    mime: str | None = None               # 如 image/png
    subdir: str | None = None             # 可选子目录，默认 saved_images


@app.post("/api/save_image")
def save_image(req: SaveImageRequest):
    """把图片（base64）保存到应用可写的本地目录，返回最终绝对路径。

    解决桌面端 pywebview 里 <a download + data URI> 无法保存的问题：
    图片由后端写盘，前端用返回路径提示用户，并可通过系统的
    os.startfile 打开对应文件夹。
    """
    import re, time, uuid
    if not req.b64:
        raise HTTPException(400, "缺少图片数据")
    try:
        raw = base64.b64decode(req.b64)
    except Exception as e:
        raise HTTPException(400, f"base64 解码失败: {e}")
    if not raw:
        raise HTTPException(400, "图片数据为空")

    subdir = req.subdir or "saved_images"
    root = config.data(subdir)
    os.makedirs(root, exist_ok=True)

    base_name = (req.filename or "image").strip() or "image"
    ext = (req.mime or "image/png").split("/")[-1].split(";")[0].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    if not base_name.lower().endswith(f".{ext}"):
        base_name += f".{ext}"

    safe = re.sub(r"[^\w.\-]", "_", base_name, flags=re.UNICODE) or "image.png"
    path = os.path.join(root, safe)
    stem, e = os.path.splitext(safe)
    i = 1
    while os.path.exists(path):
        path = os.path.join(root, f"{stem}_{i}{e}")
        i += 1
    with open(path, "wb") as f:
        f.write(raw)
    return {"ok": True, "path": path.replace("/", "\\"),
            "filename": os.path.basename(path)}


class OpenFolderRequest(BaseModel):
    path: str | None = None


def _is_container() -> bool:
    """是否运行在容器里（Docker 会创建 /.dockerenv）。"""
    if os.environ.get("MM_IN_CONTAINER"):
        return True
    return os.path.exists("/.dockerenv")


def _host_path(container_path: str) -> str:
    """把容器内路径映射回宿主机的实际路径。

    容器里没法打开宿主机的文件管理器，但可以把「宿主机上对应哪个目录」
    告诉用户（部署脚本会把该目录写进 MM_HOST_DATA_DIR）。
    """
    base = (os.environ.get("MM_HOST_DATA_DIR") or "").rstrip("/\\")
    if not base:
        return container_path
    root = config.data_root().rstrip("/\\")
    if not container_path.startswith(root):
        return container_path
    rel = container_path[len(root):].replace("/", os.sep).lstrip("/\\")
    return os.path.join(base, rel) if rel else base


# 宿主机上的「打开文件夹」小助手（open-folder-agent.ps1）会使用这两个文件：
#   .open_folder_agent    心跳，用于判断它是否在运行
#   .open_folder_request  请求，容器把路径写进去，由它真正弹出文件夹
_AGENT_BEAT = ".open_folder_agent"
_AGENT_REQ = ".open_folder_request"


def _host_agent_alive(max_age: float = 20.0) -> bool:
    """宿主机上的小助手是否在运行（靠心跳文件的更新时间判断）。"""
    try:
        beat = os.path.join(config.data_root(), _AGENT_BEAT)
        return (time.time() - os.path.getmtime(beat)) < max_age
    except Exception:
        return False


def _request_host_open(target: str) -> bool:
    """把「打开这个目录」的请求写进挂载目录，交给宿主机的小助手执行。"""
    try:
        req = os.path.join(config.data_root(), _AGENT_REQ)
        with open(req, "w", encoding="utf-8") as f:
            f.write(target)
        return True
    except Exception:
        return False


@app.post("/api/open_folder")
@app.post("/api/open_folder")
def open_folder(req: OpenFolderRequest = None):
    """在系统文件管理器中打开指定路径（默认打开 saved_images 目录）。"""
    return _open_folder_impl((req.path if req and req.path else None)
                             or config.data("saved_images"))


@app.post("/api/library/open_folder")
def library_open_folder():
    """打开「图片库」所在的文件夹。

    打开的目录就是图片实际保存的目录（image_library.DIR），
    保证「看到的」和「存进去的」是同一个地方。
    """
    return _open_folder_impl(image_library.DIR)


def _open_folder_impl(target: str) -> dict:
    """在系统文件管理器中打开一个目录（容器内交由宿主机小助手代劳）。

    - 源码/桌面方式运行：直接调用系统文件管理器
      （Windows explorer / macOS open / Linux xdg-open）
    - 容器方式运行：容器内调不起宿主机的文件管理器，改为把请求写进挂载的数据目录，
      由宿主机上的小助手代为弹出文件夹；若小助手没在运行，
      则退化成「把宿主机真实路径给用户复制」。
    """
    # 注意顺序：必须先判断是不是文件再 makedirs。
    # 若先对"文件路径"调 os.makedirs(exist_ok=True)，路径存在但不是目录时
    # 仍会抛 FileExistsError → 接口 500（前端表现为 r.json() 解析失败）。
    if os.path.isfile(target):
        target = os.path.dirname(target)
    os.makedirs(target, exist_ok=True)

    if _is_container():
        if _host_agent_alive() and _request_host_open(target):
            return {"ok": True, "opened": True, "path": target,
                    "host_path": _host_path(target), "via": "host-agent"}
        return {"ok": True, "opened": False, "path": target,
                "host_path": _host_path(target),
                "detail": "运行在容器里，无法直接打开宿主机的文件夹"}

    try:
        import subprocess
        if os.name == "nt":
            subprocess.Popen(["explorer", target.replace("/", "\\")])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return {"ok": True, "opened": True, "path": target}
    except FileNotFoundError:
        return {"ok": True, "opened": False, "path": target,
                "detail": "当前系统没找到文件管理器，请手动打开上面的路径"}
    except Exception as e:
        raise HTTPException(500, f"打开文件夹失败: {e}")


# ---------- 健康 / 配置 ----------
@app.get("/api/health")
def health():
    return client.health()


@app.get("/api/config")
def get_config():
    return {"config": config.load_config(), "defaults": config.DEFAULT_CONFIG}


@app.post("/api/config")
def update_config(body: dict):
    merged = config.load_config()
    for k, v in body.items():
        if k in config.DEFAULT_CONFIG:
            merged[k] = v
    config.save_config(merged)
    return {"ok": True, "config": merged}


@app.get("/api/net/check")
def net_check():
    """检测本机是否已连接互联网。

    供前端「联网」开关做前置校验：未联网时不允许点亮开关。
    用 TCP 连通性判断（比 HTTP 更快、不受代理/重定向干扰），
    多目标**并行**探测，任意一个通即视为已联网——断网时也能快速返回。
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    targets = [
        ("223.5.5.5", 53),        # 阿里公共 DNS
        ("www.bing.com", 443),
        ("www.baidu.com", 443),
    ]

    def _probe(host_port):
        host, port = host_port
        try:
            conn = socket.create_connection((host, port), timeout=2.0)
            conn.close()
            return host_port
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        for hit in pool.map(_probe, targets):
            if hit:
                return {"online": True, "via": f"{hit[0]}:{hit[1]}"}
    return {"online": False, "error": "无法连接互联网"}


# ---------- 模型管理 ----------
@app.get("/api/models")
def list_models():
    try:
        return {"ok": True, "models": client.list_models()}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/pull")
def pull_model(body: dict):
    name = body.get("model") or config.load_config()["default_model"]
    try:
        client.pull_model(name)
        return {"ok": True, "message": f"模型 {name} 下载完成"}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------- 对话 ----------
MAX_TOOL_ROUNDS = 10  # 单次对话内最多连续调用工具轮次，防止死循环


class _SystemPrompt:
    """WorkBuddy 风格系统提示：精简注入分层记忆，给出工具使用引导。"""

    @staticmethod
    def build(last_user_text: str):
        cfg = config.load_config()
        parts = [
            # 时间感知：让模型始终知道"今夕是何年何时"，避免说"不知道今天日期"
            "当前时间：" + _now_str(),
            "你是本地多模态助手，像一位能干的项目助理。你的所有处理都在用户本机完成，注意保护隐私。",
            "你可以调用以下工具来完成具体任务，而不仅是空谈：\n"
            "- 视觉识别（直接看，不要调用工具）：当图片/视频已经附在当前对话中（用户拖入/上传），直接用你自身的多模态视觉能力识别、描述或分析其内容即可，绝对不要为「看图」调用任何工具。只有以下四种情况才需要调用工具：\n"
            "  ① 用户想要**真实存在**的图片，如「找张 xx 的图」「搜一下 xx 图片」「xx 长什么样」「来点 xx 壁纸」→ 调用 web_image_search（到网上搜索现成的真实图片）；\n"
            "  ② 用户要求「画图/生成图片/AI 绘画/画一只 xx」这类**从零创作**→ 调用 generate_image（prompt 用英文描述），绝不能只口头描述，只有调用工具才算完成；\n"
            "  ③ 用户要求对某张图做局部修改（如「把这张图的背景改成夜晚」「给猫戴帽子」）→ 调用 edit_image；用户拖入本轮的图片优先，或填 source 为本地图片路径；\n"
            "  ④ 用户给的是一个本地文件路径、要你读取该文件 → 调用 read_file。\n"
            "- ★ 务必分清「搜图」与「生成图」：用户说「找/搜/看看」→ web_image_search（搜真实图片，不绘制）；"
            "用户说「画/生成/制作」→ generate_image（AI 创作）。两者结果来源完全不同，绝不能混淆。\n"
            "- 图片库：用户说「保存这张/存起来/收进图库」时调用 save_image_to_library（index 填本轮第几张）。\n"
            "- 文件系统：浏览用户目录、读取任意本地文件、按关键词搜索、写入或修改文件。\n"
            "- 记忆：记忆按【分区文段】整体维护（工作背景/个人背景/当前关注/近期动态…）。遇到值得长期记住的用户稳定信息、偏好、关键事实时，主动调用 remember 把对应分区的**整段文段**重写成合并新旧信息后的最新版（**自主判断，只记重要的，不要把所有问答都写入**）；"
            "当用户问「你还记得吗/我们之前说过」或需要历史信息时调用 search_memory。\n"
            "- 时间：需要当前日期时间时调用 get_time。\n"
            + (
                (
                    "- 联网搜索：用户已开启「联网」开关，你有 web_search 工具可主动联网检索。\n"
                    "  凡是涉及**最新/实时/近期**信息的问题（新闻时事、股价行情、天气、软件新版本、"
                    "赛事比分，或你不确定、知识可能已过时的内容），**绝不要回答「我无法联网」"
                    "或凭记忆猜测**，而应主动调用 web_search 获取真实网页结果，再据此用中文总结回答"
                    "并注明来源。日常闲聊、写作、翻译、代码等不需要联网的任务不要调用。\n"
                )
                if cfg.get("web_enabled")
                else (
                    "- 本机当前处于**离线模式**（用户未开启「联网」开关），无法访问互联网。"
                    "若用户需要最新信息，请提示其打开界面顶部的「联网」开关，"
                    "不要编造实时数据。\n"
                )
            )
            + "调用工具后，根据工具返回结果继续作答。能直接完成的就动手，不要只建议。",
        ]
        # 长期记忆（WorkBuddy 画像 + 检索到的记忆，精简注入）
        if cfg.get("memory_enabled", True):
            ctx = memory.build_context(last_user_text, top_k=cfg.get("memory_top_k", 5))
            if ctx:
                parts.append(ctx)
        # RAG 知识库
        if cfg.get("rag_enabled"):
            ctx = kb.build_rag_context(last_user_text, top_k=cfg.get("rag_top_k", 4))
            if ctx:
                parts.append(ctx)
        parts.append("回答请使用中文，简洁、直接、可执行。")
        return "\n\n".join(p for p in parts if p)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    model = req.model or config.load_config()["default_model"]
    cfg = config.load_config()
    images = list(req.images_b64 or [])
    _remember_image(images)          # 记住本轮图片，供后续「把这张图改成…」直接引用
    messages = list(req.messages)
    # 上下文只保留最近若干条（会话文件仍保存完整记录，前端也照常显示）
    if len(messages) > MAX_CONTEXT_MESSAGES:
        messages = messages[-MAX_CONTEXT_MESSAGES:]

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    # 简单问题收紧生成长度：把"先思考很久"压到几秒（qwen3-vl 无法真正关闭思考）。
    # 但以下情况**绝不能**收紧，否则模型来不及输出工具调用或总结（表现为"思考中断、没有回答"）：
    #   - 有图片上下文（可能要微改）
    #   - 联网模式已开启（要搜索 + 逐条引用来源，最耗 token）
    web_on = bool(cfg.get("web_enabled"))
    simple_q = (_is_simple_question(last_user)
                and not images and not _recent_image() and not web_on)
    if simple_q:
        cfg["max_tokens"] = min(int(cfg.get("max_tokens") or 2048), SIMPLE_MAX_TOKENS)
    elif web_on:
        # 联网场景给足空间：附件搜索结果 + 逐条列出来源链接会占很多 token
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 2048), WEB_MAX_TOKENS)

    # 联网搜索（仅当开启且用户明确想搜索）
    if cfg.get("web_enabled") and _wants_search(last_user):
        web_ctx = web_tools.ask_internet_search(last_user, top_k=cfg.get("web_top_k", 4))
        if web_ctx:
            messages.append({"role": "system", "content": web_ctx})

    sys_prompt = _SystemPrompt.build(last_user)

    async def gen():
        # 每轮对话的本地工作消息序列 = system + 用户历史
        working = [{"role": "system", "content": sys_prompt}] + messages
        final_text = ""
        final_thinking = ""
        session = req.session_id or ""
        tool_schemas = tools.make_schemas(cfg.get("web_enabled", False))

        for _round in range(MAX_TOOL_ROUNDS):
            # 工具调用中间轮不再重复附图片
            attach_images = images if _round == 0 else None
            try:
                resp = client.chat(working, model=model, stream=True,
                                   images_base64=attach_images, params=cfg,
                                   tools=tool_schemas)
            except ollama_client.OllamaError as e:
                yield json.dumps({"error": str(e)} | {"__end": True}) + "\n"
                return

            round_msg = {"content": "", "thinking": None, "model": model}
            tool_calls = None
            for line in resp.iter_lines(decode_unicode=False):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("error"):
                    yield json.dumps({"error": obj["error"], "__end": True}) + "\n"
                    return
                m = obj.get("message") or {}
                if m.get("thinking"):
                    round_msg["thinking"] = (round_msg["thinking"] or "") + m["thinking"]
                    final_thinking += m["thinking"]
                    # 实时透出思考增量（即使在折叠状态下也要持续刷新进度）
                    yield json.dumps({"message": {"thinking": m["thinking"]}}) + "\n"
                if m.get("content"):
                    round_msg["content"] += m["content"]
                    final_text += m["content"]
                    # 仅把用户可见的文本增量透出
                    yield json.dumps({"message": {"content": m["content"]}}) + "\n"
                if "message" in obj:
                    tc = m.get("tool_calls")
                    if tc:
                        tool_calls = tc

            if not tool_calls:
                break  # 本轮无工具调用，得到最终答复

            # ---------- 执行工具（Agent loop）----------
            # 1) 把 assistant 的 tool_calls 加入工作序列
            working.append({"role": "assistant", "content": round_msg["content"] or "",
                            "tool_calls": tool_calls})
            # 2) 逐个执行
            ui_events = []
            # images：本轮拖入的图（否则复用最近一张）
            # shown_images：本轮已展示给用户的图，供「保存到图库」工具按序号引用
            ctx = {"images": images or _recent_image(), "shown_images": []}
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                args = fn.get("arguments") or (fn.get("arguments") if isinstance(fn.get("arguments"), dict) else {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                yield json.dumps({"tool_start": {"name": name, "args": args}}) + "\n"
                mark = len(ui_events)
                result = await asyncio.to_thread(tools.dispatch, name, args, ui_events, ctx)
                # 把本次新产生的图片登记下来，后续工具（如保存到图库）可按序号引用
                for e in ui_events[mark:]:
                    if e.get("type") == "image":
                        ctx["shown_images"].append(e)
                # 注意：Ollama 的 tool 消息用 tool_name 关联调用，不是 tool_calls/tool_call_id，
                # 否则模型读不到工具返回内容（会误答"没查到/无法联网"）。
                working.append({"role": "tool", "content": result, "tool_name": name})
            # 3) 把前端副作用事件透出
            for ui in ui_events:
                yield json.dumps({"ui": ui}) + "\n"

        # 对话结束：归档历史会话（供记忆检索）+ 保存会话文件（供重启后恢复）
        full = messages + [{"role": "assistant", "content": final_text}]
        try:
            memory.save_transcript(session, full)
        except Exception:
            pass
        try:
            sessions.save_messages(session, full)
            sessions.prune(session)       # 过长则自动裁剪，避免记录无限膨胀
        except Exception:
            pass

        # 自动记忆：命中信号词时后台提炼要点写入长期记忆（不阻塞回复）。
        # 有了长期记忆兜底，久远的聊天记录才能安全清理。
        if cfg.get("auto_memorize", True) and _looks_memorable(last_user):
            try:
                threading.Thread(target=_auto_extract_memory,
                                 args=(last_user, final_text, model, cfg),
                                 daemon=True).start()
            except Exception:
                pass
        yield json.dumps({"done": True, "text": final_text, "thinking": final_thinking}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


def _wants_search(text: str) -> bool:
    """判断用户话语是否隐含需要联网搜索的意图。"""
    import re
    return bool(re.search(r"搜索|查一下|网上查|最新消息|实时新闻|搜一下|去网上|联网查", text, re.I))


# ---------- 长期记忆 API（分区文段）----------
@app.get("/api/memory")
def memory_list():
    return {"ok": True, "sections": memory.get_sections()}


@app.post("/api/memory")
def memory_add(body: dict):
    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "记忆内容不能为空")
    sec = memory.upsert(title, content)
    return {"ok": True, "section": sec}


@app.put("/api/memory/{sid}")
def memory_update(sid: str, body: dict):
    sec = memory.update(sid, title=body.get("title"), content=body.get("content"))
    if not sec:
        raise HTTPException(404, "记忆文段不存在")
    return {"ok": True, "section": sec}


@app.delete("/api/memory/{sid}")
def memory_delete(sid: str):
    if not memory.remove(sid):
        raise HTTPException(404, "记忆文段不存在")
    return {"ok": True}


@app.delete("/api/memory")
def memory_clear_all():
    memory.clear()
    return {"ok": True}


# ---------- 知识库 AI API ----------
class UploadBody(BaseModel):
    name: str
    content: str


@app.get("/api/kb")
def kb_list():
    return {"ok": True, "documents": kb.list_documents()}


@app.post("/api/kb")
def kb_save(body: UploadBody):
    doc = kb.save_document(body.name, body.content)
    return {"ok": True, "document": doc}


@app.post("/api/kb/upload")
async def kb_upload(file: UploadFile):
    content = (await file.read()).decode("utf-8", errors="replace")
    doc = kb.save_document(file.filename, content)
    return {"ok": True, "document": doc}


@app.delete("/api/kb/{name}")
def kb_delete(name: str):
    if not kb.delete_document(name):
        raise HTTPException(404, "文档不存在")
    return {"ok": True}


@app.post("/api/kb/search")
def kb_search(body: dict):
    q = body.get("query") or ""
    results = kb.search(q, top_k=body.get("top_k", 5))
    return {"ok": True, "results": results}


# ---------- 本地文件读取 API ----------
class FileReadBody(BaseModel):
    path: str
    mode: str = "auto"   # auto / text / image


@app.post("/api/file/read")
def file_read(body: FileReadBody):
    result = file_tools.read_file(body.path)
    return result


@app.post("/api/file/scan")
def file_scan(body: dict):
    """按关键词扫描目录/文件集合，返回匹配片段。"""
    paths = body.get("paths") or [body.get("path", "")]
    keywords = body.get("keywords", "")
    content = file_tools.read_by_keywords(paths, keywords)
    return {"ok": True, "content": content[:8000]}


# ---------- 视频读取 API ----------
class VideoBody(BaseModel):
    path: str


@app.post("/api/video/analyze")
def video_analyze(body: VideoBody):
    """抽取视频关键帧并让模型理解。若请求带 frames 则只做抽帧返回。
    默认返回抽帧结果；前端拿到后走 /api/chat 让 Qwen-VL 分析。"""
    result = video.extract_frames(body.path)
    return result


# ---------- 联网搜索 + 外部 API ----------
class WebSearchBody(BaseModel):
    query: str
    top_k: int = 5


@app.post("/api/web/search")
def web_search(body: WebSearchBody):
    if not config.load_config().get("web_enabled"):
        return {"ok": False, "error": "联网搜索开关未开启，请在界面打开"}
    results = web_tools.web_search(body.query, body.top_k)
    return {"ok": True, "results": results}


@app.post("/api/web/call")
def web_call(body: dict):
    """调用已配置的外部 API 工具。"""
    cfg = config.load_config()
    result = web_tools.call_external_api(cfg, body.get("tool", ""), body.get("params", {}))
    return result


@app.get("/api/web/tools")
def web_tools_list():
    return {"ok": True, "tools": web_tools.BUILTIN_TOOLS}


# ---------- 文生图 API ----------
class T2IBody(BaseModel):
    prompt: str
    negative_prompt: str = ""
    steps: int = 4
    width: int = 512
    height: int = 512
    hd: bool = False          # 是否做 4 倍超分放大（512→2048）


@app.post("/api/t2i/generate")
def t2i_generate(body: T2IBody):
    result = t2i.generate(body.prompt, body.negative_prompt, body.steps,
                          body.width, body.height, hd=body.hd)
    return result


@app.get("/api/t2i/capability")
def t2i_capability():
    """文生图能力探测：是否可用、是否支持高清放大。"""
    return {"ok": True, "hd_available": t2i.available_upscale()}


@app.post("/api/t2i/unload")
def t2i_unload():
    t2i.unload()
    return {"ok": True}


# ---------- 多会话管理 ----------
@app.get("/api/sessions")
def session_list():
    """列出所有会话（按最近更新倒序）。首次调用会自动创建一个默认会话。"""
    sessions.ensure_default()
    return {"ok": True, "sessions": sessions.list_sessions()}


@app.post("/api/sessions")
def session_create(body: dict | None = None):
    """新建会话。"""
    item = sessions.create((body or {}).get("title"))
    return {"ok": True, "session": item}


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    """读取某个会话的完整消息（前端切换会话时用于恢复）。"""
    return {"ok": True, "id": sid, "messages": sessions.get_messages(sid)}


@app.put("/api/sessions/{sid}")
def session_save(sid: str, body: dict):
    """保存会话消息（前端每次对话后调用，保证重启后能恢复）。"""
    msgs = body.get("messages") or []
    info = sessions.save_messages(sid, msgs)
    return {"ok": True, "session": info}


@app.post("/api/sessions/{sid}/rename")
def session_rename(sid: str, body: dict):
    return {"ok": sessions.rename(sid, (body or {}).get("title") or "")}


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    sessions.delete(sid)
    return {"ok": True}


# ---------- 图片库（搜到的图 / 生成的图统一留存）----------
@app.get("/api/library/images")
def library_list():
    return {"ok": True, "images": image_library.list_images(),
            "stats": image_library.stats()}


@app.post("/api/library/images")
def library_save(body: dict):
    """保存图片到图库（前端「保存到图库」按钮 / 模型 save_image_to_library 工具）。"""
    data = body.get("b64") or body.get("url") or ""
    if not data:
        return {"ok": False, "error": "缺少图片数据"}
    # 只给了远程 URL 时由后端下载，避免前端跨域
    if data.startswith("http"):
        raw = web_tools.download_image(data)
        if not raw:
            return {"ok": False, "error": "图片下载失败（可能被防盗链拦截）"}
        data = raw
    meta = image_library.save_image(data, name=body.get("name") or "",
                                    source=body.get("source") or "",
                                    origin=body.get("origin") or "web")
    if not meta.get("ok", True):
        return meta
    return {"ok": True, "image": meta}


@app.get("/api/library/images/{iid}/raw")
def library_raw(iid: str):
    """直接返回图片字节流（供前端 <img> 标签加载，避免 base64 膨胀）。"""
    p = image_library.get_path(iid)
    if not p:
        return Response(status_code=404)
    ext = os.path.splitext(p)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/png")
    with open(p, "rb") as f:
        return Response(content=f.read(), media_type=mime)


@app.get("/api/library/images/{iid}")
def library_get(iid: str):
    b64 = image_library.get_b64(iid)
    if not b64:
        return {"ok": False, "error": "图片不存在"}
    return {"ok": True, "id": iid, "b64": b64}


@app.post("/api/library/images/{iid}/rename")
def library_rename(iid: str, body: dict):
    return {"ok": image_library.rename(iid, (body or {}).get("name") or "")}


@app.delete("/api/library/images/{iid}")
def library_delete(iid: str):
    image_library.delete(iid)
    return {"ok": True}


# ---------- 语音输入（唤醒词「西派西派」+ 离线流式识别）----------
_voice_clients: set = set()
_voice_loop = None


def _broadcast_voice(event: dict) -> None:
    """把语音事件推给所有已连接的前端（从后台线程安全投递）。"""
    loop = _voice_loop
    if loop is None or loop.is_closed():
        return
    data = json.dumps(event, ensure_ascii=False)
    for ws in list(_voice_clients):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
        except Exception:
            pass


_voice = voice.get_listener(_broadcast_voice)


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    """前端语音通道：下发 start/stop/status，上游推送识别事件。"""
    global _voice_loop
    await ws.accept()
    _voice_loop = asyncio.get_running_loop()
    _voice_clients.add(ws)
    try:
        welcome = {"type": "status"}
        welcome.update(_voice.status())
        await ws.send_text(json.dumps(welcome, ensure_ascii=False))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            action = msg.get("action")
            if action == "start":
                result = _voice.start()
            elif action == "stop":
                result = _voice.stop()
            else:
                result = _voice.status()
            await ws.send_text(json.dumps({"type": "ack", "result": result},
                                          ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _voice_clients.discard(ws)


@app.get("/api/voice/status")
def voice_status():
    return _voice.status()


@app.post("/api/voice/start")
def voice_start():
    return _voice.start()


@app.post("/api/voice/stop")
def voice_stop():
    return _voice.stop()


# ---------- 前端 ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")