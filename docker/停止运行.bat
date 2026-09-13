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

REM 停掉「打开文件夹」小助手
call :stop_folder_agent

echo.
echo 已停止。数据仍保存在：%~dp0data
echo 重新启动：双击「一键部署.bat」
echo.
pause
popd
endlocal
goto :eof

REM ============================================================
REM  子过程：结束「打开文件夹」小助手
REM  1) 优先按心跳文件里的 PID 精确结束
REM  2) 兜底按命令行特征清理（务必排除自身进程，否则会把自己杀掉）
REM ============================================================
:stop_folder_agent
if exist "data\.open_folder_agent" (
    for /f "tokens=2 delims==" %%p in ('findstr "pid=" "data\.open_folder_agent" 2^>nul') do (
        taskkill /F /PID %%p >nul 2>&1
    )
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+.*open-folder-agent\.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
del /q "data\.open_folder_agent"   2>nul
del /q "data\.open_folder_request" 2>nul
echo 已停止「打开文件夹」小助手
exit /b 0
