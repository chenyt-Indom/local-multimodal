# -*- coding: utf-8 -*-
"""本地多模态助手 —— FastAPI 后端服务
用法:
    py -3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或直接:
    py -3 run.py
"""
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

import os
import base64
import io
import json
import asyncio
import datetime
from . import config, ollama_client, memory, kb, file_tools, video, web_tools, t2i, tools, voice

app = FastAPI(title="本地多模态助手", version="1.0.0")
client = ollama_client.OllamaClient()


def _now_str() -> str:
    """返回本地当前时间的中文描述，供注入系统提示，让模型具备时间感知。"""
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日（星期{wd}），{now.strftime('%H:%M:%S')}"

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


@app.post("/api/open_folder")
def open_folder(req: OpenFolderRequest = None):
    """在系统文件管理器中打开指定路径（默认打开 saved_images 目录）。"""
    target = (req.path if req and req.path else None) or config.data("saved_images")
    os.makedirs(target, exist_ok=True)
    if os.path.isfile(target):
        target = os.path.dirname(target)
    try:
        import subprocess
        subprocess.Popen(["explorer", target.replace("/", "\\")])
        return {"ok": True, "path": target}
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
            "- 视觉识别（直接看，不要调用工具）：当图片/视频已经附在当前对话中（用户拖入/上传），直接用你自身的多模态视觉能力识别、描述或分析其内容即可，绝对不要为「看图」调用任何工具。只有以下三种情况才需要调用工具：\n"
            "  ① 用户明确要求「画图/生成图片/制作配图/AI绘画/画一只××」→ 调用 generate_image 工具真实生成（prompt 用英文描述），绝不能只口头描述，只有调用工具才算完成；\n"
            "  ② 用户明确要求对某张图做局部修改（如「把这张图的背景改成夜晚」「给猫戴帽子」「换个颜色」）→ 调用 edit_image；用户拖入本轮的图片优先，或填 source 为本地图片路径；\n"
            "  ③ 用户给的是一个本地文件路径、要你读取该文件 → 调用 read_file。\n"
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
    messages = list(req.messages)

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

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
            ctx = {"images": images}  # 本轮对话拖入/附带的图片，供 edit_image 等工具使用
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
                result = await asyncio.to_thread(tools.dispatch, name, args, ui_events, ctx)
                # 注意：Ollama 的 tool 消息用 tool_name 关联调用，不是 tool_calls/tool_call_id，
                # 否则模型读不到工具返回内容（会误答"没查到/无法联网"）。
                working.append({"role": "tool", "content": result, "tool_name": name})
            # 3) 把前端副作用事件透出
            for ui in ui_events:
                yield json.dumps({"ui": ui}) + "\n"

        # 对话结束：归档历史会话（长期记忆由 AI 用 remember 工具自主更新文段）
        try:
            memory.save_transcript(session, messages + [{"role": "assistant", "content": final_text}])
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


@app.post("/api/t2i/generate")
def t2i_generate(body: T2IBody):
    result = t2i.generate(body.prompt, body.negative_prompt, body.steps,
                          body.width, body.height)
    return result


@app.post("/api/t2i/unload")
def t2i_unload():
    t2i.unload()
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