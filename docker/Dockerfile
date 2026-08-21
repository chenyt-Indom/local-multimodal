# -*- 本地多模态助手 —— 完整自包含镜像（含 Ollama + Qwen3-VL-8B + 后端 + 前端）
# 完全离线：镜像内已内置模型权重，无需联网下载。
# 构建时需要把模型权重放到 docker/models（见 docker/build-context.sh）。

FROM ollama/ollama:latest AS base

# ---------- 安装后端运行环境 ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-pip python3-venv libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

# ---------- 复制后端与前端 ----------
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# ---------- 复制内置模型权重（自包含，离线可跑） ----------
# 目录: docker/models → 容器 /root/.ollama/models
# Windows 下模型名含冒号，构建时由 build-context 脚本组织好 models 内容。
COPY --chown=root:root docker/models/ /root/.ollama/models/

# ---------- 安装 Python 依赖 ----------
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# ---------- 启动入口 ----------
COPY docker/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000 11434
ENTRYPOINT ["/docker-entrypoint.sh"]