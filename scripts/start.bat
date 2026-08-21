@echo off
chcp 65001 >nul
REM 本地多模态助手 - 启动脚本
echo ============================================
echo   正在启动本地多模态助手...
echo ============================================
echo [*] 确保 Ollama 在运行（若未运行会自动拉起）...
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul || start "" "ollama" app.exe
where ollama >nul 2>&1 && (start "" ollama || echo [i] 已启动 Ollama)

echo [*] 启动后端服务 http://127.0.0.1:8000
py -3 run.py
pause