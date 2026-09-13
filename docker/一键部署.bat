@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
pushd "%~dp0"
title 本地多模态助手 · 一键部署

echo ============================================================
echo            本地多模态助手 · 一键部署
echo ============================================================
echo.

REM ================= 1. 环境检查 =================
docker version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到可用的 Docker 引擎。
    echo.
    echo        请先安装 Docker Desktop 并确保它已启动：
    echo        https://www.docker.com/products/docker-desktop/
    echo.
    pause
    exit /b 1
)
echo [1/5] Docker 引擎正常

REM 优先用新版 compose 子命令，兼容独立版 docker-compose
set "DC=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    docker-compose version >nul 2>&1
    if errorlevel 1 (
        echo [错误] 未找到 docker compose，请升级 Docker Desktop。
        pause & exit /b 1
    )
    set "DC=docker-compose"
)

REM ================= 2. 选择版本 =================
REM 命令行参数可强制指定： 一键部署.bat lite  /  一键部署.bat full
set "MODE=%~1"
if not defined MODE (
    REM 自动：离线包里有完整版就用完整版，否则用精简版
    if exist "images\local-multimodal-full.tar" (set "MODE=full") else (set "MODE=lite")
)
if /i not "%MODE%"=="full" if /i not "%MODE%"=="lite" set "MODE=lite"

if /i "%MODE%"=="full" (
    set "TITLE=完整版（离线自包含，含对话+绘图模型约 10GB，首次导入需数分钟）"
) else (
    set "TITLE=精简版（体积小，首次启动联网下载对话模型约 6GB）"
)
echo [2/5] 部署版本：!MODE!  -  !TITLE!

REM ================= 3. 显卡自动适配 =================
set "GPU="
where nvidia-smi >nul 2>&1 && nvidia-smi >nul 2>&1 && set "GPU=1"
set "FILES=-f compose.%MODE%.yml"
if defined GPU (
    set "FILES=!FILES! -f compose.%MODE%.gpu.yml"
    echo [3/5] 检测到 NVIDIA 显卡，已启用 GPU 加速
) else (
    echo [3/5] 未检测到 NVIDIA 显卡，使用 CPU 模式（功能完整，速度稍慢）
)

REM ================= 4. 准备镜像 =================
set "IMG=local-multimodal-%MODE%:latest"
docker image inspect "!IMG!" >nul 2>&1
if errorlevel 1 (
    if exist "images\local-multimodal-%MODE%.tar" (
        echo [4/5] 首次运行，正在导入离线镜像（体积较大，请耐心等待）...
        docker load -i "images\local-multimodal-%MODE%.tar"
        if errorlevel 1 (
            echo [错误] 镜像导入失败，请确认 images\local-multimodal-%MODE%.tar 完整。
            pause & exit /b 1
        )
    ) else (
        echo [4/5] 未找到离线镜像包，改为从源码构建（需要联网下载依赖）...
        %DC% !FILES! build
        if errorlevel 1 ( echo [错误] 构建失败。 & pause & exit /b 1 )
    )
) else (
    echo [4/5] 本地已有镜像 !IMG!
)

REM ================= 5. 启动服务 =================
echo [5/5] 启动服务...
%DC% !FILES! up -d
if errorlevel 1 (
    echo.
    echo [错误] 启动失败。可执行「查看状态.bat」查看容器日志。
    pause & exit /b 1
)

echo.
echo 正在等待服务就绪（精简版首次需下载模型，可能要几分钟到十几分钟）...
set "READY="
for /l %%i in (1,1,150) do (
    if not defined READY (
        curl -fsS -m 3 http://127.0.0.1:8000/api/health >nul 2>&1 && set "READY=1"
        if not defined READY timeout /t 2 /nobreak >nul
    )
)

if defined READY (
    echo.
    echo ============================================================
    echo   部署完成！正在打开浏览器：http://127.0.0.1:8000
    echo ============================================================
    start "" http://127.0.0.1:8000
) else (
    echo.
    echo [提示] 服务尚未就绪，可能仍在下载模型或初始化。
    echo        稍等片刻后手动打开：http://127.0.0.1:8000
    echo        查看进度：运行「查看状态.bat」
)

echo.
echo 停止运行：双击「停止运行.bat」
echo 数据保存在：%~dp0data
echo.
pause
popd
endlocal
