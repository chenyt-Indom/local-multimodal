# -*- coding: utf-8 -*-
"""全局配置：本地多模态助手
所有可在界面中调整的参数集中在此，便于“包括环境与参数”的定制需求。
"""
import json
import os

# 项目根目录（兼容 PyInstaller 打包运行）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    # Ollama 本地服务地址（请保证 ollama serve 已启动）
    "ollama_url": "http://localhost:11434",
    # 默认模型，需先用 ollama pull qwen3-vl:8b 下载
    "default_model": "qwen3-vl:8b",
    # 推理参数
    "temperature": 0.7,
    # 12GB 显卡建议 4096：Q4 模型仅占~6GB，为上下文/浏览器留足显存余量
    "num_ctx": 4096,
    # Qwen3-VL 思考模式会先占用大量 token，放大配额避免回答被思考吃光
    "max_tokens": 2048,
}


def load_config() -> dict:
    """读取配置文件；不存在则写入默认配置。"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """把用户修改后的配置写回文件，实现参数持久化。"""
    clean = {k: cfg.get(k) for k in DEFAULT_CONFIG}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)