@echo off
chcp 936 >nul
setlocal
pushd "%~dp0"
title 本地多模态助手 · 运行状态

echo ============================================================
echo            本地多模态助手 · 运行状态
echo ============================================================
echo.

echo ---------- 容器 ----------
docker ps -a --filter "name=mm-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo.

echo ---------- 界面服务 ----------
curl -fsS -m 5 http://127.0.0.1:8000/api/health
if errorlevel 1 (echo [未就绪] 服务尚未启动完成) else (echo. & echo [OK] 后端已就绪)
echo.

echo ---------- 宿主机 Ollama（直连模式用）----------
curl -fsS -m 5 http://127.0.0.1:11434/api/version
if errorlevel 1 (echo [未运行] 直连模式下需要本机 Ollama 正在运行) else (echo.)
echo.

echo ---------- 应用日志（30 行）----------
docker logs --tail 30 mm-app 2>&1
echo.

echo ---------- 磁盘占用 ----------
docker system df
echo.

echo 数据目录：%~dp0data
echo.
pause
popd
endlocal
