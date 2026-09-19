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
echo [1/4] 停止「打开文件夹」小助手...
call :stop_folder_agent

echo [2/4] 停止并删除容器...
docker compose -f compose.yml --profile bundled down -v 2>nul

echo [3/4] 删除镜像...
REM ?? 四个镜像都要删。原来只删了前两个，GPU 版（约 10GB）和模型镜像（约 38GB）
REM    会留在 Docker 里 —— 用户点了「完全卸载」却发现磁盘没释放多少，很难查。
docker image rm local-multimodal-app:latest 2>nul
docker image rm local-multimodal-app:gpu 2>nul
docker image rm local-multimodal-ollama:latest 2>nul
docker image rm local-multimodal-models:latest 2>nul

echo [4/4] 删除本地数据目录...
if exist "data" rd /s /q "data"

echo.
echo 卸载完成。离线包 images\、模型 models\ 未删除，可手动清理。
echo         （Docker 里为本次部署腾出的空间可用 docker system prune 进一步回收）
echo.
pause
popd
endlocal
goto :eof

:stop_folder_agent
if exist "data\.open_folder_agent" (
    for /f "tokens=2 delims==" %%p in ('findstr "pid=" "data\.open_folder_agent" 2^>nul') do (
        taskkill /F /PID %%p >nul 2>&1
    )
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+.*open-folder-agent\.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
del /q "data\.open_folder_agent"   2>nul
del /q "data\.open_folder_request" 2>nul
exit /b 0
