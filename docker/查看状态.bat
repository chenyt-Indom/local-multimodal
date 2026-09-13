@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
title 本地多模态助手 · 运行状态

echo ============================================================
echo            本地多模态助手 · 运行状态
echo ============================================================
echo.

echo ---------- 容器状态 ----------
docker ps -a --filter "name=mm-" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo.

echo ---------- 界面服务健康检查 ----------
curl -fsS -m 5 http://127.0.0.1:8000/api/health && echo. && echo [OK] 后端已就绪 || echo [未就绪] 服务尚未启动完成
echo.

echo ---------- 最近日志（30 行）----------
docker logs --tail 30 mm-app 2>&1
echo.

echo ---------- 磁盘占用 ----------
docker system df
echo.
pause
popd
endlocal
