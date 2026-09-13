#!/bin/bash
# -*- 精简版镜像里的 Ollama 初始化
# 首次启动时把对话模型拉到本地卷里，之后启动直接复用（离线可用）。
set -u

MODEL="${OLLAMA_MODEL:-qwen3-vl:8b}"

echo "[ollama-init] 启动 Ollama 服务 ..."
ollama serve >/var/log/ollama.log 2>&1 &
OLLAMA_PID=$!

cleanup() { kill "$OLLAMA_PID" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup TERM INT

for i in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
        echo "[ollama-init] 服务就绪（用时 ${i}s）"
        break
    fi
    sleep 1
done

# 模型是否已在本地卷里？
if ollama list 2>/dev/null | grep -q "${MODEL%%:*}"; then
    echo "[ollama-init] 模型 ${MODEL} 已存在，跳过下载"
else
    echo "[ollama-init] 首次运行，开始拉取模型 ${MODEL}（约 6GB，请耐心等待）..."
    if ollama pull "$MODEL"; then
        echo "[ollama-init] 模型拉取完成 ✅"
    else
        echo "[ollama-init] 模型拉取失败，将稍后重试（可手动执行：docker exec -it mm-ollama ollama pull $MODEL）"
    fi
fi

echo "[ollama-init] 模型清单："
ollama list || true

wait "$OLLAMA_PID"
