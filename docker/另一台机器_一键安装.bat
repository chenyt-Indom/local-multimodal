@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 本地多模态助手 · 一键安装（换机器用）

set "REG=ccr.ccs.tencentyun.com/bendiai"
set "SUITE=%REG%/multimodal-deploy:latest"

echo ============================================================
echo      本地多模态助手 · 一键安装（换成这台机器）
echo ============================================================
echo.
echo  这个脚本会全自动做完：
echo    1) 检查 Docker 是否装好、引擎是否已启动
echo    2) 从腾讯云拉取「部署套件」（约 188MB，不需要登录任何账号）
echo    3) 把部署脚本解出来，然后自动开始正式部署（约 25GB）
echo.
echo  全程不用输账号密码，也不用去网页下载任何东西。
echo.

REM ============ 1. 目录 ============
set "HERE=%~dp0"
if "!HERE:~-1!"=="\" set "HERE=!HERE:~0,-1!"
set "TARGET=!HERE!"
echo  当前脚本所在目录：!HERE!
set /p "TARGET=安装到哪个目录？（直接回车 = 就用这个）："
if "!TARGET!"=="" set "TARGET=!HERE!"
REM 去掉用户可能粘进来的首尾引号
set "TARGET=!TARGET:"=!"
if not exist "!TARGET!" (
    mkdir "!TARGET!" 2>nul
    if not exist "!TARGET!" (
        echo.
        echo [X] 建不了目录：!TARGET!
        pause & exit /b 1
    )
)
pushd "!TARGET!"
echo  安装目录：%CD%
echo.

REM ============ 2. Docker 检查 ============
where docker >nul 2>&1
if errorlevel 1 goto :no_docker
docker version >nul 2>&1
if errorlevel 1 goto :no_engine
echo [1/3] Docker 正常

REM ============ 3. 拉部署套件 ============
echo [2/3] 正在拉取部署套件（约 188MB，第一次要等一会儿）...
echo       来源：%SUITE%
docker pull "%SUITE%"
if errorlevel 1 goto :pull_fail

REM ============ 4. 解出脚本 ============
echo [3/3] 正在把脚本解到：%CD%
docker run --rm -v "%CD%:/out" "%SUITE%" sh -c "cp -r /deploy/. /out/"
if errorlevel 1 goto :extract_fail

if not exist "一键部署.bat" goto :extract_fail

echo.
echo ============================================================
echo   套件已就位。下面开始正式部署（首次约 25GB，要等一段时间）
echo   期间请勿关闭 Docker Desktop、不要断网。
echo ============================================================
echo.
call "一键部署.bat"

echo.
echo 安装流程结束。若一切正常，浏览器会自动打开 http://127.0.0.1:8000
echo.
popd
pause
endlocal
exit /b 0

REM ============================================================
:no_docker
echo.
echo [X] 这台机器还没装 Docker Desktop。
echo.
echo     下载地址：https://www.docker.com/products/docker-desktop/
echo     安装时保持默认选项，装完**重启一次电脑**，再双击本脚本。
echo.
echo     （如果这台机器不能上网，请改用 U 盘拷「本地多模态助手-Docker」整个文件夹，
echo       里面有 installers\DockerDesktopInstaller.exe 可直接装。）
echo.
popd
pause
exit /b 1

:no_engine
echo.
echo [X] docker 命令在，但引擎没启动。
echo.
echo     请先打开 Docker Desktop，等左下角小鲸鱼图标变绿（显示 Engine running），
echo     再双击本脚本。
echo.
popd
pause
exit /b 1

:pull_fail
echo.
echo [X] 拉取部署套件失败。常见原因：
echo     1) 网络不通 —— 打开浏览器试试能不能上百度
echo     2) 磁盘满了 —— 先清出 40GB 以上
echo     3) 镜像仓库被改成私有了 —— 去腾讯云控制台把仓库「类型」改成公有
echo.
echo     已经拉下来的部分会保留，修好之后重新双击本脚本即可接着来。
echo.
popd
pause
exit /b 1

:extract_fail
echo.
echo [X] 解压部署套件失败（没看到「一键部署.bat」）。
echo     重新双击本脚本再试一次；还是不行就把这段截图发我。
echo.
popd
pause
exit /b 1
