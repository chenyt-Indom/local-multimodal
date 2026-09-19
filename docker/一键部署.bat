@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
pushd "%~dp0"
title 本地多模态助手 · 一键部署

REM 镜像仓库（在线部署模式用；离线包模式用不到它）
REM   ?? 换账号/仓库时改这里
set "REG=ccr.ccs.tencentyun.com/bendiai"

echo ============================================================
echo            本地多模态助手 · 一键部署
echo ============================================================
echo.

REM ============ 1. 环境检查 ============
docker version >nul 2>&1
if not errorlevel 1 goto :docker_ready
call :no_docker
exit /b 1

:docker_ready
set "DC=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    docker-compose version >nul 2>&1
    if errorlevel 1 ( echo [错误] 未找到 docker compose，请升级 Docker Desktop。 & pause & exit /b 1 )
    set "DC=docker-compose"
)
echo [1/6] Docker 引擎正常
REM 磁盘空间：镜像 7GB + 模型 19GB，解包和运行还要留余量，建议 40GB
set "FREEGB=0"
for /f %%s in ('powershell -NoProfile -Command "try{[math]::Round((Get-PSDrive (Split-Path -Qualifier $env:CD)).Free/1GB,0)}catch{0}"') do set "FREEGB=%%s"
if %FREEGB% GEQ 40 goto :disk_ok
if %FREEGB% GEQ 25 (
    echo        [注意] 磁盘只剩 %FREEGB% GB，勉强够用，建议再清理一些。
) else (
    echo        [警告] 磁盘只剩 %FREEGB% GB，很可能不够！
    echo               本包需要约 30 GB（镜像 7GB + 模型 19GB + 运行时余量）。
    echo               建议先清理磁盘，或换一台空间足够的机器。
    choice /c YN /n /m "        确定要继续吗？[Y/N] "
    if errorlevel 2 exit /b 1
)
:disk_ok

REM ============ 2. 显卡自动适配 ============
set "FILES=-f compose.yml"
set "HASGPU="
where nvidia-smi >nul 2>&1 && nvidia-smi >nul 2>&1 && set "HASGPU=1"
if defined HASGPU (
    set "FILES=!FILES! -f compose.gpu.yml"
    echo [2/6] 检测到 NVIDIA 显卡，已启用 GPU 加速
) else (
    echo [2/6] 未检测到 NVIDIA 显卡，使用 CPU 模式
    echo        [重要] CPU 模式下实测：14B 代码模型约 0.4 字/秒，一句话要等好几分钟；
    echo               8B 模型也很慢。功能是全的，但体验会明显受限。
    echo               建议换一台有 NVIDIA 独显的机器运行。
)

REM ---- 应用镜像二选一 ----
REM 两者功能完全相同，区别只在 torch：CUDA 版体积大，但绘图（文生图）能用显卡。
REM 对话/看图走的是 Ollama，与这里选哪个镜像无关。
set "APP_IMG=local-multimodal-app:latest"
set "APP_TAR=images\local-multimodal-app.tar"
if defined HASGPU (
    if exist "images\local-multimodal-app-gpu.tar" (
        set "APP_IMG=local-multimodal-app:gpu"
        set "APP_TAR=images\local-multimodal-app-gpu.tar"
        echo        应用镜像：CUDA 版（绘图走显卡）
    ) else (
        echo        应用镜像：CPU 版（没找到 GPU 镜像，绘图较慢）
    )
) else (
    echo        应用镜像：CPU 版
)
set "APP_IMG_SET=!APP_IMG!"

REM ============ 3. 选择 Ollama 模式 ============
REM 可用参数强制指定： 一键部署.bat direct  /  一键部署.bat bundled
set "MODE=%~1"
if not defined MODE (
    REM 宿主机已有 Ollama 在跑 → 直接连它，零下载
    curl -fsS -m 3 http://127.0.0.1:11434/api/version >nul 2>&1
    if errorlevel 1 (set "MODE=bundled") else (set "MODE=direct")
)

set "PROFILE="
if /i "%MODE%"=="direct" (
    echo [3/6] Ollama 模式：直连宿主机（零下载，用本机已装的 Ollama）
    set "MM_URL_SET=http://host.docker.internal:11434"
    set "MM_IMG_SET="
) else (
    echo [3/6] Ollama 模式：容器自带
    set "PROFILE=--profile bundled"
    set "MM_URL_SET=http://ollama:11434"
    set "MM_IMG_SET=local-multimodal-ollama:latest"
)
call :write_env

REM ============ 4. 准备镜像 ============
set "NEED_BUILD="
docker image inspect !APP_IMG! >nul 2>&1
if errorlevel 1 (
    if exist "!APP_TAR!" (
        echo [4/6] 正在导入应用镜像（首次运行需等待，文件较大）...
        docker load -i "!APP_TAR!"
        if errorlevel 1 ( echo [错误] 镜像导入失败。 & pause & exit /b 1 )
    ) else (
        REM 本地既没有镜像、也没有离线 tar —— 先试着从镜像仓库拉（比从源码构建快得多，
        REM 也不用装编译环境）。拉不到才退回源码构建。
        echo [4/6] 本地没有离线镜像，正在从镜像仓库拉取...
        call :try_registry
        if errorlevel 1 (
            echo        镜像仓库不可用，改为从源码构建（需联网，较慢）...
            set "NEED_BUILD=1"
        )
    )
) else (
    echo [4/6] 应用镜像已存在：!APP_IMG!
)

if /i "%MODE%"=="bundled" (
    docker image inspect local-multimodal-ollama:latest >nul 2>&1
    if errorlevel 1 (
        call :ensure_ollama
        if errorlevel 1 (
            echo.
            echo [错误] 无法获取 Ollama 镜像。
            echo        可改用直连模式（用本机已装的 Ollama）：一键部署.bat direct
            pause & exit /b 1
        )
    )
)

if defined NEED_BUILD (
    %DC% !FILES! !PROFILE! build
    if errorlevel 1 ( echo [错误] 构建失败。 & pause & exit /b 1 )
)

REM ============ 4.5 准备模型 ============
REM 离线包模式：模型随包带来（models/ 目录），这里什么都不用做。
REM 在线模式：  模型在 multimodal-models 镜像里，**必须提取出来** ——
REM             compose 挂载的是 ./models/ollama，不提取的话容器里是空的，
REM             表现为「模型列表空、问什么都报找不到模型」。
call :ensure_models
if errorlevel 1 (
    echo.
    echo [错误] 模型没准备好，启动起来也是空的。
    echo        可以手动重试：docker pull %REG%/multimodal-models:latest
    pause & exit /b 1
)

REM ============ 5. 启动服务 ============
echo [5/6] 启动服务...
%DC% !FILES! !PROFILE! up -d
if errorlevel 1 ( echo. & echo [错误] 启动失败，可运行「查看状态.bat」查看原因。 & pause & exit /b 1 )

REM 顺带拉起「打开文件夹」小助手：容器里调不起宿主机的资源管理器，由它代劳
call :start_folder_agent

REM ============ 6. 等待就绪并打开浏览器 ============
echo [6/6] 等待服务就绪...
set "READY="
for /l %%i in (1,1,60) do (
    if not defined READY (
        curl -fsS -m 3 http://127.0.0.1:8000/api/health >nul 2>&1 && set "READY=1"
        if not defined READY >nul ping -n 3 127.0.0.1
    )
)

echo.
if defined READY (
    echo ============================================================
    echo   部署完成！正在打开浏览器：http://127.0.0.1:8000
    echo ============================================================
    start "" http://127.0.0.1:8000
) else (
    echo [提示] 服务还在启动中，稍等片刻手动打开：http://127.0.0.1:8000
    echo        查看进度：运行「查看状态.bat」
)

echo.
echo 模式：%MODE%    镜像：!APP_IMG!    停止：双击「停止运行.bat」    数据目录：%~dp0data
echo.
pause
popd
endlocal
goto :eof

REM ============================================================
REM  子过程：写 .env
REM  必须用「UTF-8 + 无 BOM」：compose 按 UTF-8 读 .env，
REM  而 cmd 的 echo 只会按本地代码页(GBK)写，中文路径会变乱码。
REM  命令行里不出现任何中文，路径由它自己取当前目录得到。
REM ============================================================
:write_env
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=(Get-Location).Path.Replace('\','/')+'/data'; $c=@('# auto-generated by deploy script','APP_PORT=8000',('APP_IMAGE='+$env:APP_IMG_SET),('MM_OLLAMA_URL='+$env:MM_URL_SET),'MM_MODEL=qwen3-vl:8b',('MM_HOST_DATA_DIR='+$p)); if ($env:MM_IMG_SET) { $c += ('OLLAMA_IMAGE='+$env:MM_IMG_SET) }; [IO.File]::WriteAllLines('.env',$c,(New-Object Text.UTF8Encoding($false)))"
exit /b 0

REM ============================================================
REM  子过程：启动宿主机「打开文件夹」小助手（隐藏窗口，常驻）
REM ============================================================
:start_folder_agent
if not exist "open-folder-agent.ps1" exit /b 0
set "MM_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $env:MM_ROOT 'open-folder-agent.ps1')) -WindowStyle Hidden" >nul 2>&1
exit /b 0

REM ============================================================
REM  子过程：从镜像仓库拉应用镜像
REM  拉下来后**打成本地同名 tag** —— compose 里写的是
REM  local-multimodal-app:latest / :gpu，改 tag 就不用动 compose。
REM ============================================================
:try_registry
if /i "!APP_IMG!"=="local-multimodal-app:gpu" (set "RIMG=multimodal-app:gpu") else (set "RIMG=multimodal-app:latest")
echo       拉取 %REG%/!RIMG! ...
docker pull %REG%/!RIMG!
if errorlevel 1 (
    REM GPU 版没传上去 / 拉不动时，退回 CPU 版（功能完全一样，只是绘图慢）
    if /i "!RIMG!"=="multimodal-app:gpu" (
        echo       GPU 版拉取失败，退回 CPU 版（功能一样，绘图慢一些）...
        set "RIMG=multimodal-app:latest"
        set "APP_IMG=local-multimodal-app:latest"
        docker pull %REG%/!RIMG!
        if errorlevel 1 exit /b 1
    ) else (
        exit /b 1
    )
)
docker tag %REG%/!RIMG! !APP_IMG!
REM 顺手也拉一份 CPU 版：界面上的「切换设备」要用到，换机器时也不用再拉
docker pull %REG%/multimodal-app:latest >nul 2>&1 && docker tag %REG%/multimodal-app:latest local-multimodal-app:latest
exit /b 0

REM ============================================================
REM  子过程：确保模型就位（在线部署的关键一步）
REM  离线包模式：models/ 目录里本来就有，直接返回。
REM  在线模式：  模型在 multimodal-models 镜像里，提取到本地 models/ 目录 ——
REM             compose 挂载的是 ./models/ollama，不提取容器里就是空的。
REM ============================================================
:ensure_models
if exist "models\ollama\manifests" exit /b 0
if exist "models\sd-turbo\model_index.json" exit /b 0
echo       本地还没有模型，正在从镜像仓库提取（约 18GB，第一次会久一点）...
docker pull %REG%/multimodal-models:latest
if errorlevel 1 exit /b 1
docker rm -f mm-models-tmp >nul 2>&1
docker create --name mm-models-tmp %REG%/multimodal-models:latest >nul 2>&1
if errorlevel 1 exit /b 1
docker cp mm-models-tmp:/models/. "models\"
set "CPERR=%errorlevel%"
docker rm -f mm-models-tmp >nul 2>&1
if not "%CPERR%"=="0" exit /b 1
echo       模型提取完成
exit /b 0

REM ============================================================
REM  子过程：确保 local-multimodal-ollama 镜像就绪
REM  顺序：离线包 tar → 国内镜像源 → 官方源
REM ============================================================
:ensure_ollama
if not exist "images\local-multimodal-ollama.tar" goto :eo_registry
echo       正在导入离线 Ollama 镜像...
docker load -i "images\local-multimodal-ollama.tar"
if errorlevel 1 goto :eo_registry
exit /b 0

:eo_registry
REM 镜像仓库里有现成的（就是从这个包推上去的），比走 Docker Hub 快得多也不容易失败
echo       正在从镜像仓库拉取 Ollama 运行时（约 9GB，请耐心等待）...
docker pull %REG%/multimodal-ollama:latest
if errorlevel 1 goto :eo_pull
docker tag %REG%/multimodal-ollama:latest local-multimodal-ollama:latest
exit /b 0

:eo_pull
echo       未找到离线镜像，正在从镜像源拉取（约 9GB，请耐心等待）...
for %%M in (docker.1ms.run docker.1panel.live docker.xuanyuan.me) do call :try_mirror %%M
docker image inspect local-multimodal-ollama:latest >nul 2>&1
if not errorlevel 1 exit /b 0
echo       镜像源均未成功，尝试官方源（国内可能非常慢）...
docker pull ollama/ollama:latest
if errorlevel 1 exit /b 1
docker tag ollama/ollama:latest local-multimodal-ollama:latest
exit /b 0

:try_mirror
docker image inspect local-multimodal-ollama:latest >nul 2>&1
if not errorlevel 1 exit /b 0
echo       尝试镜像源 %1 ...
docker pull %1/ollama/ollama:latest
if errorlevel 1 exit /b 1
docker tag %1/ollama/ollama:latest local-multimodal-ollama:latest
exit /b 0

REM ============================================================
REM  子过程：没装 Docker 时的引导
REM  包里有安装程序就直接引导安装，装卸完再跑一次本脚本即可。
REM ============================================================
:no_docker
echo [1/6] 这台机器还没有可用的 Docker 引擎。
echo.
if not exist "installers\DockerDesktopInstaller.exe" goto :nd_nopkg
echo   好消息：安装包里自带了 Docker Desktop 安装程序——
echo     %~dp0installers\DockerDesktopInstaller.exe
echo.
echo   请先安装它（安装时保持默认选项，务必勾选使用 WSL2 后端），
echo   装完启动 Docker Desktop，等左下角变绿，然后**再双击一次本脚本**继续部署。
echo.
choice /c YN /n /m "   现在打开安装程序吗？[Y/N] "
if errorlevel 2 goto :nd_end
start "" "%~dp0installers\DockerDesktopInstaller.exe"
echo.
echo   已打开安装程序。装好 Docker Desktop 之后，请再运行一次本脚本。
goto :nd_end

:nd_nopkg
echo   请先安装并启动 Docker Desktop：
echo     https://www.docker.com/products/docker-desktop/
echo   装好之后**再双击一次本脚本**继续部署。

:nd_end
echo.
pause
exit /b 1
