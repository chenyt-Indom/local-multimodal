@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
pushd "%~dp0"
title 本地多模态助手 · 一键部署

echo ============================================================
echo            本地多模态助手 · 一键部署
echo ============================================================
echo.

REM ============ 1. 环境检查 ============
docker version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到可用的 Docker 引擎。
    echo        请先安装并启动 Docker Desktop：https://www.docker.com/products/docker-desktop/
    echo.
    pause
    exit /b 1
)
set "DC=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    docker-compose version >nul 2>&1
    if errorlevel 1 ( echo [错误] 未找到 docker compose，请升级 Docker Desktop。 & pause & exit /b 1 )
    set "DC=docker-compose"
)
echo [1/6] Docker 引擎正常

REM ============ 2. 显卡自动适配 ============
set "FILES=-f compose.yml"
set "HASGPU="
where nvidia-smi >nul 2>&1 && nvidia-smi >nul 2>&1 && set "HASGPU=1"
if defined HASGPU (
    set "FILES=!FILES! -f compose.gpu.yml"
    echo [2/6] 检测到 NVIDIA 显卡，已启用 GPU 加速
) else (
    echo [2/6] 未检测到 NVIDIA 显卡，使用 CPU 模式（功能完整，速度稍慢）
)

REM ============ 3. 选择 Ollama 模式 ============
REM 可用参数强制指定： 一键部署.bat direct  /  一键部署.bat bundled
set "MODE=%~1"
if not defined MODE (
    REM 宿主机已有 Ollama 在跑 → 直接连它，零下载
    curl -fsS -m 3 http://127.0.0.1:11434/api/version >nul 2>&1
    if errorlevel 1 (set "MODE=bundled") else (set "MODE=direct")
)

set "PROFILE="
if /i "%MODE%"=="direct" (
    echo [3/6] Ollama 模式：直连宿主机（零下载，用本机已装的 Ollama）
    > ".env" echo # 由一键部署脚本自动生成
    >>".env" echo APP_PORT=8000
    >>".env" echo MM_OLLAMA_URL=http://host.docker.internal:11434
    >>".env" echo MM_MODEL=qwen3-vl:8b
) else (
    echo [3/6] Ollama 模式：容器自带（需已有 Ollama 镜像）
    set "PROFILE=--profile bundled"
    > ".env" echo # 由一键部署脚本自动生成
    >>".env" echo APP_PORT=8000
    >>".env" echo MM_OLLAMA_URL=http://ollama:11434
    >>".env" echo MM_MODEL=qwen3-vl:8b
    >>".env" echo OLLAMA_IMAGE=local-multimodal-ollama:latest
)

REM ============ 4. 准备镜像 ============
set "NEED_BUILD="
docker image inspect local-multimodal-app:latest >nul 2>&1
if errorlevel 1 (
    if exist "images\local-multimodal-app.tar" (
        echo [4/6] 首次运行，正在导入应用镜像（请耐心等待）...
        docker load -i "images\local-multimodal-app.tar"
        if errorlevel 1 ( echo [错误] 镜像导入失败。 & pause & exit /b 1 )
    ) else (
        echo [4/6] 未找到离线镜像，改为从源码构建（需联网）...
        set "NEED_BUILD=1"
    )
) else (
    echo [4/6] 应用镜像已存在
)

if /i "%MODE%"=="bundled" (
    docker image inspect local-multimodal-ollama:latest >nul 2>&1
    if errorlevel 1 (
        if exist "images\local-multimodal-ollama.tar" (
            echo       正在导入 Ollama 镜像...
            docker load -i "images\local-multimodal-ollama.tar"
        ) else (
            echo       本地无 Ollama 镜像，尝试从镜像仓库拉取...
            docker pull ollama/ollama:latest
            if errorlevel 1 (
                echo.
                echo [提示] 拉取失败。国内网络可改用镜像源重试：
                echo        docker pull docker.1ms.run/ollama/ollama:latest
                echo        docker tag  docker.1ms.run/ollama/ollama:latest local-multimodal-ollama:latest
                echo.
                echo        或改用直连模式（用本机已装的 Ollama）：一键部署.bat direct
                pause & exit /b 1
            )
            docker tag ollama/ollama:latest local-multimodal-ollama:latest
        )
    )
)

if defined NEED_BUILD (
    %DC% !FILES! !PROFILE! build
    if errorlevel 1 ( echo [错误] 构建失败。 & pause & exit /b 1 )
)

REM ============ 5. 启动服务 ============
echo [5/6] 启动服务...
%DC% !FILES! !PROFILE! up -d
if errorlevel 1 ( echo. & echo [错误] 启动失败，可运行「查看状态.bat」查看原因。 & pause & exit /b 1 )

REM ============ 6. 等待就绪并打开浏览器 ============
echo [6/6] 等待服务就绪...
set "READY="
for /l %%i in (1,1,60) do (
    if not defined READY (
        curl -fsS -m 3 http://127.0.0.1:8000/api/health >nul 2>&1 && set "READY=1"
        if not defined READY >nul ping -n 3 127.0.0.1
    )
)

echo.
if defined READY (
    echo ============================================================
    echo   部署完成！正在打开浏览器：http://127.0.0.1:8000
    echo ============================================================
    start "" http://127.0.0.1:8000
) else (
    echo [提示] 服务还在启动中，稍等片刻手动打开：http://127.0.0.1:8000
    echo        查看进度：运行「查看状态.bat」
)

echo.
echo 模式：%MODE%    停止：双击「停止运行.bat」    数据目录：%~dp0data
echo.
pause
popd
endlocal
