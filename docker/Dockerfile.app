# -*- 本地多模态助手 · 应用镜像
# 内容：FastAPI 后端 + 前端 + Python 运行时 + 绘图/超分运行时（torch、diffusers、spandrel）
# 不包含任何模型权重 —— 模型通过挂载 ./models 目录提供，避免在磁盘上存两份。
#
# 构建（上下文为包内的 build 目录）：
#   docker build -f docker/Dockerfile.app -t local-multimodal-app:latest .
# 绘图想走显卡（体积更大）：
#   docker build -f docker/Dockerfile.app \
#     --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124 \
#     -t local-multimodal-app:latest .
FROM python:3.12-slim

# torch 来源：默认 CPU 版（任何机器都能跑）；可换成 cu124 走 NVIDIA 显卡
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
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
RUN pip install --index-url ${TORCH_INDEX} torch torchvision
COPY docker/requirements-image.txt ./
RUN pip install --index-url "${PIP_INDEX}" -r requirements-image.txt

# ---------- 应用代码 ----------
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 运行数据与模型挂载点
RUN mkdir -p /data /opt/app/sd_model /opt/app/esrgan

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=8 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
