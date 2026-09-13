@echo off
chcp 936 >nul
setlocal
pushd "%~dp0"
title 本地多模态助手 · 停止运行

echo ============================================================
echo            本地多模态助手 · 停止运行
echo ============================================================
echo.
echo 正在停止容器（你的聊天记录、记忆库、图片库都会保留）...

REM --profile bundled 一并覆盖自带 Ollama 的情况
docker compose -f compose.yml --profile bundled down

echo.
echo 已停止。数据仍保存在：%~dp0data
echo 重新启动：双击「一键部署.bat」
echo.
pause
popd
endlocal
