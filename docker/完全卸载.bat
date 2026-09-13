@echo off
chcp 936 >nul
setlocal
pushd "%~dp0"
title 本地多模态助手 · 完全卸载

echo ============================================================
echo            本地多模态助手 · 完全卸载
echo ============================================================
echo.
echo 本操作将删除：容器、镜像，以及全部聊天记录/记忆库/图片库数据。
echo.
set "ANS="
set /p ANS=确认继续请输入 YES（其它任意键取消）:
if /i not "%ANS%"=="YES" (
    echo 已取消。
    pause & exit /b 0
)

echo.
echo [1/3] 停止并删除容器...
docker compose -f compose.yml --profile bundled down -v 2>nul

echo [2/3] 删除镜像...
docker image rm local-multimodal-app:latest 2>nul
docker image rm local-multimodal-ollama:latest 2>nul

echo [3/3] 删除本地数据目录...
if exist "data" rd /s /q "data"

echo.
echo 卸载完成。离线包 images\、模型 models\ 未删除，可手动清理。
echo.
pause
popd
endlocal
