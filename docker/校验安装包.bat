@echo off
chcp 936 >nul
pushd "%~dp0"
title 本地多模态助手 · 校验安装包
echo.
call :run %*
popd
exit /b 0

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "校验安装包.ps1" %*
exit /b 0
