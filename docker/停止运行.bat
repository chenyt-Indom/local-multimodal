@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
title 本地多模态助手 · 停止运行

echo ============================================================
echo            本地多模态助手 · 停止运行
echo ============================================================
echo.

echo 正在停止容器（数据与镜像都会保留）...
docker compose -f compose.full.yml down 2>nul
docker compose -f compose.lite.yml down 2>nul

echo.
echo 已停止。记忆库 / 会话记录 / 图片库 均保存在 data 目录，不会被删除。
echo 重新使用：双击「一键部署.bat」
echo.
pause
popd
endlocal
