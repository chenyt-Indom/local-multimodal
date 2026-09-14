# -*- coding: utf-8 -*-
"""Ollama 本地 API 客户端。
完全本地运行：仅与 http://localhost:11434 通信，不走公网、数据不出机。
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
    def health(self) -> dict:
        """返回 Ollama 是否在线、默认模型是否已下载。"""
        online = False
        try:
            self._req("GET", "/api/version").json()
            online = True
        except OllamaError:
            online = False
        model_ready = False
        if online:
            installed = [m["name"] for m in self.list_models()]
            model_ready = config.load_config()["default_model"] in installed
        return {"online": online, "model_ready": model_ready,
                "model": config.load_config()["default_model"]}

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