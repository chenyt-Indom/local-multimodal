@echo off
chcp 65001 >nul
REM 下载默认模型 qwen3-vl:8b（约 8GB，只需执行一次，之后完全离线）
echo ============================================
echo   下载多模态模型  qwen3-vl:8b
echo   体积约 8GB，请耐心等待...
echo ============================================

ollama pull qwen3-vl:8b
if errorlevel 1 (
    echo [-] 下载失败。请确认 Ollama 已安装并运行（ollama serve）。
    pause
    exit /b 1
)

echo.
echo [+] 模型下载完成！现在可以运行  start.bat 启动应用。
pause