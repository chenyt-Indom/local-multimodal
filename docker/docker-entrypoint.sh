#!/bin/bash
# -*- 本地多模态助手 —— 容器启动入口
# 同时启动 Ollama 服务与 FastAPI 后端。

set -e

# 让 Ollama 读取镜像内内置的模型目录
export OLLAMA_MODELS=/root/.ollama/models
export OLLAMA_HOST=0.0.0.0

echo "[entrypoint] 启动 Ollama 服务..."
ollama serve &
OLLAMA_PID=$!

# 等待 Ollama 就绪
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
        echo "[entrypoint] Ollama 已就绪，检测模型："
        ollama list
        break
    fi
    sleep 1
done

echo "[entrypoint] 启动后端服务 (127.0.0.1:8000)..."
cd /opt/app
exec python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000