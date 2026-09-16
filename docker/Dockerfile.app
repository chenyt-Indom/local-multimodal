# -*- 本地多模态助手 · 应用镜像
# 内容：FastAPI 后端 + 前端 + Python 运行时 + 绘图/超分运行时（torch、diffusers、spandrel）
# 不包含任何模型权重 —— 模型通过挂载 ./models 目录提供，避免在磁盘上存两份。
#
# 构建（上下文为包内的 build 目录）：
#   docker build -f docker/Dockerfile.app -t local-multimodal-app:latest .
# 绘图想走显卡（体积会大很多，约 3~4 GB）：
#   docker build -f docker/Dockerfile.app \
#     --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu130 \
#     -t local-multimodal-app:latest .
#
# ⚠️ CUDA 版本怎么选（实测踩过坑）：
#   torch 轮子是按 GPU **算力架构**编译的，选错了会在推理时报
#   "no kernel image is available for execution on the device"。
#     · RTX 50 系（Blackwell，sm_120）→ 必须 cu128 及以上，**cu124 不行**，用 cu130
#     · RTX 40 系（Ada，sm_89）/ 30 系（Ampere，sm_86）→ cu124 / cu128 均可
#   不确定就用 cu130（向下兼容较老的架构）。
FROM python:3.12-slim

# torch 来源：默认 CPU 版（任何机器都能跑）；可换成 cu130 走 NVIDIA 显卡
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
# 固定 torch / torchvision 版本，保证 CPU 版与 CUDA 版**除后端外完全一致**，
# 避免因版本漂移引入 diffusers / transformers 的兼容问题。
ARG TORCH_VER=2.14.0
ARG TORCHVISION_VER=0.29.0
# pip 源，默认官方；国内可传 --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    MM_DATA_DIR=/data \
    SD_MODEL_DIR=/opt/app/sd_model \
    ESRGAN_MODEL=/opt/app/esrgan/RealESRGAN_x4plus.pth

WORKDIR /opt/app

# 运行期系统依赖：curl 供健康检查，libgl1/libglib2.0-0 供 OpenCV/Pillow 处理图像
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libgl1 libglib2.0-0 tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ---------- 应用依赖 ----------
COPY docker/requirements-app.txt ./
RUN pip install --index-url "${PIP_INDEX}" -r requirements-app.txt

# ---------- 绘图依赖 ----------
# torch 与 torchvision 必须从**同一个源**安装：
#   PyPI 上的 torchvision 是针对 CUDA 版 torch 编译的，与 CPU 版 torch 混装会报
#   "operator torchvision::nms does not exist"，进而让 transformers / diffusers
#   在 import 阶段就崩 —— 表现为「图片微改 / 文生图 引擎加载失败」。
# 把这一对先装好，后面 pip 会认为依赖已满足，不会再把它们换掉。
RUN pip install --index-url ${TORCH_INDEX} \
        "torch==${TORCH_VER}" "torchvision==${TORCHVISION_VER}"
COPY docker/requirements-image.txt ./
RUN pip install --index-url "${PIP_INDEX}" -r requirements-image.txt

# ---------- 应用代码 ----------
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# ---------- 内置的真 VS Code（code-server）----------
# 和本机源码模式保持一致：开发台的「🧩 VS Code」按钮要在容器里也能用。
# 绿色包自带 Node，解压即用。
#
# ⚠️ **优先用构建上下文里预置的包**，别再默认走网络：
#    这是"离线一键部署包"，构建期联网是自相矛盾的 —— 而且实测会挂：
#    直连 github release 是 302 到 objects.githubusercontent.com，
#    网络一差就卡住 40+ 分钟（`--retry 3` 只会更慢）。
#    把 code-server-<版本>-linux-amd64.tar.gz 放进 docker/vendor-assets/ 即可，
#    构建变成纯本地、几秒钟过；没预置才回退到联网下载。
# ⚠️ 不要写成 `COPY docker/code-server-*.tar.gz` —— 文件不在时会让**整个构建失败**；
#    改成 COPY 一个**目录**（总存在），再用 shell 判断。
ARG CS_VER=4.137.0
COPY docker/vendor-assets/ /tmp/vendor-assets/
RUN set -eux; \
    if [ -f /tmp/vendor-assets/code-server-linux-amd64.tar.gz ]; then \
        echo "== 使用 vendor-assets 里预置的 code-server（不联网）=="; \
        cp /tmp/vendor-assets/code-server-linux-amd64.tar.gz /tmp/cs.tgz; \
    else \
        echo "== 未预置，联网下载 code-server =="; \
        curl -fsSL --connect-timeout 15 --max-time 300 --retry 2 -o /tmp/cs.tgz \
          "https://gh-proxy.com/https://github.com/coder/code-server/releases/download/v${CS_VER}/code-server-${CS_VER}-linux-amd64.tar.gz" \
        || curl -fsSL --connect-timeout 15 --max-time 300 --retry 2 -o /tmp/cs.tgz \
          "https://github.com/coder/code-server/releases/download/v${CS_VER}/code-server-${CS_VER}-linux-amd64.tar.gz"; \
    fi; \
    mkdir -p /opt/app/vendor; \
    tar -xzf /tmp/cs.tgz -C /opt/app/vendor; \
    mv "/opt/app/vendor/code-server-${CS_VER}-linux-amd64" /opt/app/vendor/code-server; \
    chmod +x /opt/app/vendor/code-server/bin/code-server; \
    rm -rf /tmp/cs.tgz /tmp/vendor-assets

# 运行数据与模型挂载点
RUN mkdir -p /data /opt/app/sd_model /opt/app/esrgan

EXPOSE 8000 8810
# 容器里 code-server 必须绑 0.0.0.0 才能被宿主的端口映射碰到；
# 安全性靠 `docker run -p 127.0.0.1:8810:8810` 只映射到宿主回环来保证。
ENV MM_CODE_BIND=0.0.0.0

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=8 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
