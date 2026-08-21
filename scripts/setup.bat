@echo off
chcp 65001 >nul
REM 本地多模态助手 - 一键环境搭建脚本
REM 安装 Python 依赖

echo ============================================
echo   本地多模态助手 - 环境搭建
echo ============================================
echo.

REM 检查 Python
py -3 --version >nul 2>&1
if errorlevel 1 (
    echo [-] 未找到 Python，请先安装 Python 3.10+（勾选 Add to PATH）
    echo     下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [*] 安装 Python 依赖（FastAPI / uvicorn / requests / pillow）...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
    echo [-] 依赖安装失败，请检查网络或 pip 源。
    pause
    exit /b 1
)

echo.
echo [*] 正在检测 Ollama ...
ollama --version >nul 2>&1
if errorlevel 1 (
    echo [!] 未检测到 Ollama。
    echo     请手动安装: 打开 https://ollama.com 下载并安装
    echo     或执行: winget install Ollama.Ollama
) else (
    echo [+] Ollama 已安装。
)

echo.
echo ============================================
echo   环境搭建完成！
echo   下一步：
echo     1. 若未安装 Ollama，请先安装
echo     2. 运行  pull_model.bat  下载模型（约8GB，只需一次）
echo     3. 运行  start.bat  启动应用
echo ============================================
pause