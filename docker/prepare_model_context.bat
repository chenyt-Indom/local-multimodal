@echo off
chcp 65001 >nul
REM -*- 准备 Docker 构建上下文：把本机已下载的 Ollama 模型复制到 docker/models
REM 这样构建镜像时无需联网重新下载 6GB 模型权重，且镜像离线自包含。
setlocal
cd /d "%~dp0.."

set "SRC=%USERPROFILE%\.ollama\models"
set "DST=%CD%\docker\models"

echo 源模型目录: %SRC%
echo 目标目录  : %DST%

if not exist "%SRC%\manifests" (
    echo [错误] 未找到本机模型 directory: %SRC%
    exit /b 1
)

if not exist "%DST%" mkdir "%DST%"

echo 正在复制模型（约 6GB，请耐心等待）...
xcopy "%SRC%\manifests" "%DST%\manifests\" /E /I /Y /Q
xcopy "%SRC%\blobs" "%DST%\blobs\" /E /I /Y /Q

echo [完成] 模型已复制到 docker\models，可执行 docker\start_docker.bat 构建。
echo 提示：若镜像已构建过，后续无需重复执行本脚本。
endlocal