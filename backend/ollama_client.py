# -*- coding: utf-8 -*-
"""Ollama 本地 API 客户端。
完全本地运行：仅与 http://127.0.0.1:11434 通信，不走公网、数据不出机。
"""
import base64
import io
import requests
from PIL import Image
from . import config


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self):
        self.url = config.load_config()["ollama_url"]

    def _req(self, method, path, **kwargs):
        try:
            resp = requests.request(method, self.url + path, timeout=600, **kwargs)
        except requests.exceptions.ConnectionError:
            raise OllamaError("无法连接 Ollama，请先启动 ollama serve")
        if resp.status_code != 200:
            detail = resp.text[:300]
            raise OllamaError(f"Ollama 返回错误 {resp.status_code}: {detail}")
        return resp

    # ---------- 健康检查 ----------
    @staticmethod
    def _brief(model: dict | None) -> dict | None:
        """把 Ollama 报的一个模型，摘成界面要显示的「版本 + 参数规模」。

        ⚠️ 2026-09-26 加。**必须有这个**：以前界面只显示"模型就绪/未下载"，
        连自己在用哪个模型、多大参数都看不到 —— 换过模型（sd-turbo→SDXL、
        qwen2.5-coder→qwen3-coder）之后用户无从确认到底生效了没有。
        这里一律**从 Ollama 现读**（`/api/tags` 的 details），不写死任何名字，
        所以以后不管换成什么模型，界面显示的都会自动跟着变。
        """
        if not model:
            return None
        det = model.get("details") or {}
        size = model.get("size") or 0
        return {
            "name": str(model.get("name") or ""),
            # 参数规模（Ollama 报的 8.8B / 30.5B 这种）
            "parameter_size": str(det.get("parameter_size") or ""),
            # 量化档（Q4_K_M …）—— 直接决定它多大、多准
            "quantization_level": str(det.get("quantization_level") or ""),
            # 架构家族（qwen3vl / qwen3moe …），MoE 与 dense 一眼可分
            "family": str(det.get("family") or ""),
            "families": det.get("families") or [],
            "size_bytes": size,
            "size_text": ("%.1f GB" % (size / 1e9)) if size else "",
        }

    def health(self) -> dict:
        """返回 Ollama 是否在线、默认模型是否已下载，**以及实际模型的版本与参数规模**。"""
        online = False
        try:
            self._req("GET", "/api/version").json()
            online = True
        except OllamaError:
            online = False

        cfg = config.load_config()
        default = str(cfg.get("default_model") or "")
        code_model = str(cfg.get("code_model") or "").strip()

        models: list = []
        if online:
            try:
                models = self.list_models()
            except OllamaError:
                models = []
        by_name = {str(m.get("name") or ""): m for m in models}

        def _ready(name: str) -> bool:
            # 允许"只写了仓库名没写 tag"的情况（Ollama 会补 :latest）
            return bool(name) and (name in by_name or (name + ":latest") in by_name)

        return {
            "online": online,
            "model_ready": _ready(default),
            "model": default,
            # 界面把这两段直接显示出来（模型名 · 参数量 · 量化 · 体积）
            "model_info": self._brief(by_name.get(default) or by_name.get(default + ":latest")),
            "code_model": code_model,
            "code_model_ready": _ready(code_model) if code_model else False,
            "code_model_info": (self._brief(by_name.get(code_model)
                                            or by_name.get(code_model + ":latest"))
                                if code_model else None),
            # 本机装了哪些（界面的"还没下载 xxx"提示要用）
            "installed": sorted(by_name.keys()),
        }

    # ---------- 模型管理 ----------
    def list_models(self) -> list:
        data = self._req("GET", "/api/tags").json()
        return data.get("models", [])

    def pull_model(self, name: str):
        """通过流式方式拉取模型的子进程无法在纯 HTTP 中直接流式，
        这里用同步 JSON 请求简化处理。界面负责展示状态。"""
        return self._req("POST", "/api/pull", json={"name": name, "stream": True})

    # ---------- 对话 ----------
    def chat(self, messages: list, model: str, stream: bool = True,
             images_base64: list | None = None, params: dict | None = None,
             tools: list | None = None):
        """发送对话。images_base64 为图片 base64 字符串列表（附加到最后一条 user 消息）。
        tools 为函数调用 schema 列表，用于 Agent 工具循环。

        ⚠️ **num_ctx 一律取全局配置，忽略调用方传入的值。**
        Ollama 只要发现 num_ctx 与当前已加载的不同，就会**卸载并重载模型**
        （实测约 5 秒），而且**重载会中断正在进行的生成**。
        曾经因为后台记忆提取用了 4096、聊天用 8192，导致：
          聊天 → 记忆(重载) → 用户再发消息(又重载) → 生成被打断
        用户看到的就是「模型加载一半、思考一半、没有回答」。
        与其要求每个调用点自觉对齐，不如在这里统一收口——多一个调用点
        也不会再踩这个坑。num_predict / temperature 仍可按调用区分。
        """
        payload = {"model": model, "messages": messages, "stream": stream}
        # ⚠️ **必须显式给 keep_alive**：Ollama 默认只保活 5 分钟，超时就卸载模型。
        # 不给的话，用户隔几分钟再发消息就要重新加载（实测冷启动 4.5 秒起步），
        # 体感就是"发出去半天没反应"。值走配置（model_keep_alive，默认 30m）。
        try:
            _ka = (config.load_config() or {}).get("model_keep_alive")
        except Exception:
            _ka = None
        if _ka:
            payload["keep_alive"] = _ka
        if params:
            try:
                fixed_ctx = int(config.load_config().get("num_ctx") or 8192)
            except Exception:
                fixed_ctx = int(params.get("num_ctx") or 8192)
            payload["options"] = {
                "temperature": params.get("temperature"),
                "num_ctx": fixed_ctx,
                "num_predict": params.get("max_tokens"),
            }
            # 采样参数：**只在显式给了值时才传**。
            # ⚠️ 传 None 会覆盖掉 Ollama 自己的默认值（反而更糟），所以逐个判空。
            # 这两个是压「思考打转」（同一段话换个连接词反复写）的关键：
            #   · repeat_last_n —— Ollama 默认只有 64 个 token（≈40 汉字），
            #     而打转是**段落级**的（重复段本身就 30~60 字），64 的窗口盖不住；
            #   · repeat_penalty —— 抬到 1.15 让"再写一遍"的代价变大。
            for _k in ("repeat_penalty", "repeat_last_n",
                       "presence_penalty", "frequency_penalty", "top_p", "top_k"):
                _v = params.get(_k)
                if _v not in (None, "", 0):
                    payload["options"][_k] = _v
        if tools:
            payload["tools"] = tools
        # 把图片附加到最后一条 user 消息
        if images_base64:
            last_user = next(x for x in reversed(messages) if x["role"] == "user")
            last_user.setdefault("images", []).extend(images_base64)
        if stream:
            return self._req("POST", "/api/chat", json=payload, stream=True)
        return self._req("POST", "/api/chat", json=payload)


def encode_image_bytes(data: bytes) -> str:
    """把二进制图片编码为 base64 字符串（Ollama 需要的格式）。"""
    return base64.b64encode(data).decode("utf-8")


def load_image_b64(path_or_bytes) -> str:
    """从文件路径或 bytes 读取图片并编码为 base64。"""
    if isinstance(path_or_bytes, str):
        with open(path_or_bytes, "rb") as f:
            raw = f.read()
    else:
        raw = path_or_bytes
    # 压缩过大的图片，避免请求体过大
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    max_side = 1024
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")