#!/bin/bash
# -*- 本地多模态助手 · 完整版启动入口
# 在同一个容器内拉起 Ollama 与 FastAPI 后端（数据不出机、完全离线）。
set -e

export OLLAMA_MODELS="${OLLAMA_MODELS:-/root/.ollama/models}"
export OLLAMA_HOST="${OLLAMA_HOST:-0.0.0.0}"

echo "=============================================="
echo "  本地多模态助手 · 完整版容器启动"
echo "=============================================="

# ---------- 1. 启动 Ollama ----------
echo "[1/3] 启动 Ollama 服务 ..."
ollama serve >/var/log/ollama.log 2>&1 &
OLLAMA_PID=$!

cleanup() {
    echo "[entrypoint] 收到退出信号，正在关闭 ..."
    kill "$OLLAMA_PID" 2>/dev/null || true
    kill "$APP_PID"   2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup TERM INT

# 等待 Ollama 就绪（最多 60 秒）
for i in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
        echo "[entrypoint] Ollama 已就绪（用时 ${i}s）"
        break
    fi
    if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
        echo "[entrypoint] Ollama 进程已退出，日志末尾："
        tail -n 20 /var/log/ollama.log || true
        exit 1
    fi
    sleep 1
done

echo "[entrypoint] 镜像内置模型："
ollama list || true

# ---------- 2. 启动后端 ----------
echo "[2/3] 启动后端服务 0.0.0.0:8000 ..."
cd /opt/app
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
APP_PID=$!

# ---------- 3. 输出访问地址 ----------
echo "[3/3] 就绪，浏览器访问 http://127.0.0.1:8000"
echo "=============================================="

wait -n "$OLLAMA_PID" "$APP_PID" || true
cleanup
