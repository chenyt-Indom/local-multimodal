@echo off
chcp 65001 >nul
REM -*- 本地多模态助手 —— 停止 Docker 容器（保留镜像与数据）
setlocal
cd /d "%~dp0"
docker compose down
echo 容器已停止。如需彻底删除应用与镜像数据，请执行：
echo   docker compose down -v
echo   docker image rm local-multimodal:latest
pause
endlocal