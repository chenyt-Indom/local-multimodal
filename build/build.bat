@echo off
chcp 65001 >nul
REM 打包为 Windows 单文件应用程序
echo ============================================
echo   打包本地多模态助手为 exe
echo ============================================

where py >nul 2>&1
if errorlevel 1 ( echo [-] 未找到 Python & pause & exit /b 1 )

echo [*] 安装 PyInstaller ...
py -3 -m pip install pyinstaller --quiet

echo [*] 开始打包（可能需要几分钟）...
cd /d "%~dp0.."
py -3 -m PyInstaller "build/本地多模态助手.spec" --noconfirm

if errorlevel 1 (
    echo [-] 打包失败
    pause
    exit /b 1
)

echo.
echo [+] 打包完成！可执行文件位于: dist/本地多模态助手.exe
echo     把该 exe 复制到任意目录即可运行（仍需要已安装 Ollama 与模型）。
pause