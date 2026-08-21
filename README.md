# 本地多模态助手（本地多模态）

一个**完全本地、单机离线运行**的办公多模态 AI 助手，基于阿里开源 **Qwen3-VL-8B**。
支持：日常聊天、写作（周报/邮件/文案）、文章总结、图片理解。

> 🔒 **完全本地：数据不出机、免费、无任何外部 API 调用。**

![模型](https://img.shields.io/badge/模型-Qwen3--VL--8B-blue)
![许可](https://img.shields.io/badge/许可-Apache%202.0-green)
![离线](https://img.shields.io/badge/运行-100%25本地离线-orange)

---

## ✨ 功能

- 💬 多轮对话，支持流式输出（打字机效果）
- 🖼 图片理解：上传/粘贴图片，自动描述或识别内容
- 📝 办公场景：写周报、邮件、文案、总结文章
- ⚙️ 可调参数：温度、上下文长度、最大生成长度（持久化保存）
- 📦 模型管理：一键检测、下载默认模型
- 🚀 可打包成单文件 exe 归档为独立应用

## 目录结构

```
.
├── backend/            # FastAPI 后端（Ollama 本地 API 客户端、配置）
├── frontend/           # 前端界面（HTML / CSS / JS）
├── scripts/            # 一键脚本（环境搭建 / 拉模型 / 启动）
├── build/              # PyInstaller 打包配置
├── run.py              # 启动入口
├── requirements.txt    # Python 依赖
└── config.json         # 运行时参数（首次启动自动生成）
```

## 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11（Linux/macOS 亦可手动运行） |
| 显卡 | NVIDIA GPU，显存 ≥ 8GB（推荐 16GB，测试环境 RTX 5070 Ti 16GB） |
| 内存 | ≥ 16GB |
| 硬盘 | 预留 ≥ 20GB（模型约 8-16GB） |

## ⚡ 快速开始（五步）

### 1. 安装 Ollama

方式一（推荐）：前往 https://ollama.com 下载安装包，双击安装。

方式二（命令行）：
```bash
winget install Ollama.Ollama
```

### 2. 安装 Python（若需从源码运行）

前往 https://www.python.org 安装 Python 3.10+，勾选 **Add to PATH**。

### 3. 安装后端依赖

```bash
python -m pip install -r requirements.txt
```

### 4. 下载模型（约 8GB，只需一次，之后完全离线）

```bash
ollama pull qwen3-vl:8b
```

### 5. 启动应用

```bash
python run.py
```
浏览器自动打开 `http://127.0.0.1:8000`。

---

也可以直接双击 `scripts/` 下的一键脚本：
- `setup.bat` —— 一键安装依赖
- `pull_model.bat` —— 下载模型
- `start.bat` —— 启动应用

## 🧩 打包为应用程序（exe）

```bash
cd build
build.bat
```
完成后生成 `dist/本地多模态助手.exe`，单文件、无需安装，复制到任意目录即可运行（仍需已装 Ollama 与模型）。

## 🐳 Docker 一键部署（完整自包含镜像）

镜像内置 **Ollama + Qwen3-VL-8B 模型 + 后端 + 前端**，约 20GB，拉取后无需下载模型、完全离线开箱即用。

```bash
cd docker
.\prepare_model_context.bat   # 可选：把本机已下载的模型复制为构建上下文（避免联网重下6GB）
.\start_docker.bat             # 双击/命令行：构建镜像并启动容器，随后自动打开浏览器
```

- 界面地址：http://127.0.0.1:8000
- 停止容器：`docker\stop_docker.bat`
- 手动构建：`docker compose up -d --build`
- 镜像导出/导入（便于离线分发到其他机器）：
  ```bash
  docker save -o local-multimodal-latest.tar local-multimodal:latest   # 导出
  docker load -i local-multimodal-latest.tar                            # 导入
  ```

> 说明：无 NVIDIA 容器运行时（WSL2 未配 GPU 透传）时会以 **CPU 模式**推理，功能完整但较慢；配好 GPU 后自动加速。

## 🔧 参数说明

配置集中在 `config.json`（前端「推理参数」可在线修改并自动保存）：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ollama_url` | `http://localhost:11434` | Ollama 本地服务地址 |
| `default_model` | `qwen3-vl:8b` | 默认推理模型 |
| `temperature` | `0.7` | 采样温度，越低越严谨 |
| `num_ctx` | `8192` | 上下文窗口长度 |
| `max_tokens` | `1024` | 单次最大生成 token 数 |

## 📈 性能参考（RTX 5070 Ti 16GB）

- 首次加载：约 10-30 秒
- 聊天速度：约 20-40 token/秒（流畅）
- 显存占用：FP8 量化约 12GB

## 🛠 常见问题

**启动后浏览器无法连接 Ollama？**
确保 Ollama 已启动（任务栏有 Ollama 图标），或运行 `ollama serve`。

**模型未就绪？**
在界面「模型管理」中点击「开始下载」，或执行 `ollama pull qwen3-vl:8b`。

**显存不足？**
在 `config.json` 中调低 `num_ctx`（如 4096），或改用更小的量化模型。

## 📄 许可

- 本工具：Apache 2.0
- 模型 Qwen3-VL-8B：Apache 2.0（免费商用）

## ☁️ 部署方案文档

本项目的部署思路来自《本地多模态模型部署方案》，核心结论：
选 Qwen3-VL-8B（FP8 约 12GB 显存）+ Ollama 部署，实现 5 分钟装机、永久离线使用。