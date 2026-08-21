@echo off
chcp 65001 >nul
REM -*- 本地多模态助手 —— 双击启动 Docker 应用
setlocal
cd /d "%~dp0"

if not exist "models\manifests\registry.ollama.ai\library\qwen3-vl" (
    echo 尚未准备模型上下文，正在执行 prepare_model_context.bat ...
    call prepare_model_context.bat
    if errorlevel 1 ( echo [失败] 模型未就绪 & pause & exit /b 1 )
)

echo [1/3] 构建并启动 Docker 容器（首次构建需数分钟）...
docker compose up -d --build

echo [2/3] 等待后端就绪...
timeout /t 6 /nobreak >nul

echo [3/3] 打开浏览器...
start "" http://127.0.0.1:8000

echo.
echo 应用已启动：http://127.0.0.1:8000
echo 停止应用：运行 docker\stop_docker.bat
endlocal