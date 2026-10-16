# -*- coding: utf-8 -*-
r"""给便携整合包生成"用户看得见"的入口文件（启动 / 建快捷方式 / 说明）。

⚠️ `.bat` 必须是 **GBK + CRLF**（Windows 命令提示符按 ANSI 读批处理，
写 UTF-8 会乱码）。所以这里不用 Write 工具，统一用 python 的
`encode("gbk")` 写盘。中文说明文档则用 UTF-8。

用法：python scripts\make_bundle_launchers.py [包根]
"""
import io
import os
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BUNDLE = sys.argv[1] if len(sys.argv) > 1 else r"E:\本地多模态助手"

START_BAT = r"""@echo off
chcp 936 >nul
title 本地多模态助手
cd /d "%~dp0"
if not exist "%~dp0runtime\python\pythonw.exe" (
  echo [错误] 找不到 runtime\python\pythonw.exe
  echo        包不完整，请重新解压安装包。
  pause
  exit /b 1
)
if not exist "%~dp0app\run.py" (
  echo [错误] 找不到 app\run.py
  echo        包不完整，请重新解压安装包。
  pause
  exit /b 1
)
echo 正在启动本地多模态助手...
echo   包目录 : %~dp0
echo   首次启动要加载 30B 模型，可能等 1~2 分钟，请耐心。
echo.
start "" "%~dp0runtime\python\pythonw.exe" "%~dp0启动窗口.pyw"
exit /b 0
"""

SHORTCUT_BAT = r"""@echo off
chcp 936 >nul
cd /d "%~dp0"
echo 正在创建快捷方式（桌面 + 开始菜单）...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; " ^
  "$t = '%~dp0启动.bat'; $w = '%~dp0'; " ^
  "$d = [Environment]::GetFolderPath('Desktop') + '\本地多模态助手.lnk'; " ^
  "$s = $ws.CreateShortcut($d); $s.TargetPath = $t; $s.WorkingDirectory = $w; $s.Description = '本地多模态助手'; $s.Save(); " ^
  "$m = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\本地多模态助手.lnk'; " ^
  "$s2 = $ws.CreateShortcut($m); $s2.TargetPath = $t; $s2.WorkingDirectory = $w; $s2.Description = '本地多模态助手'; $s2.Save(); " ^
  "Write-Host '已创建: ' $d; Write-Host '已创建: ' $m"
echo.
echo 完成！以后双击桌面的「本地多模态助手」即可启动。
pause
"""

SELFCHECK_BAT = r"""@echo off
chcp 936 >nul
title 本地多模态助手 · 自检
cd /d "%~dp0"
echo ============================================================
echo   本地多模态助手 · 安装自检
echo ============================================================
echo.
set OK=1
call :chk "app\run.py"                          "应用代码"
call :chk "runtime\python\pythonw.exe"          "Python 运行时"
call :chk "runtime\python\Lib\site-packages\torch" "torch / diffusers 依赖"
call :chk "ollama\ollama.exe"                   "内置 Ollama"
call :chk "models\ollama\manifests"             "对话模型清单"
call :chk "models\sd\sdxl-base\model_index.json"   "文生图权重"
call :chk "models\sd\sdxl-inpaint\model_index.json" "局部重绘权重"
call :chk "data\config.json"                    "配置文件"
echo.
if "%OK%"=="1" (
  echo [通过] 包是完整的，双击「启动.bat」即可。
) else (
  echo [失败] 上面标 [缺失] 的东西没解压全，请重新解压安装包。
)
echo.
pause
exit /b 0

:chk
if exist %1 (
  echo   [正常] %~2
) else (
  echo   [缺失] %~2   ^(%1^)
  set OK=0
)
exit /b 0
"""

README = """# 本地多模态助手 · 便携整合包

这是一个**自包含**的整体：代码、Python 运行时、Ollama、全部模型都在这个目录里。
整块拷到别的盘 / 别的机器（盘符变了也没关系）都能直接用。

## 三步用起来

1. 双击 **`启动.bat`**
   （首次启动要加载 30B 模型，等 1~2 分钟是正常的）
2. 想建桌面图标：双击 **`创建快捷方式.bat`**（一次性）
3. 怀疑包不完整：双击 **`检查自检.bat`**

## 目录说明

| 目录 | 是什么 |
|---|---|
| `app\\` | 应用代码（后端 backend\\ + 前端 frontend\\ + run.py） |
| `runtime\\python\\` | Python 运行时，**已含**全部依赖（torch / diffusers / fastapi …） |
| `ollama\\` | 内置的 Ollama（由启动器自己拉起，**不用装系统版**） |
| `models\\ollama\\` | 对话模型（qwen3-vl-think:30b / qwen3-coder:30b），约 38 GB |
| `models\\sd\\` | 文生图与局部重绘权重（SDXL base + inpainting），约 14 GB |
| `models\\rembg\\` | 抠图模型 |
| `models\\asr\\` | 语音识别模型 |
| `models\\upscale\\` | 高清超分模型（RealESRGAN） |
| `data\\` | 记忆 / 知识库 / 文库 / config.json（**你自己的数据都在这**） |

## 为什么要把模型放进来

以前模型散落在 `E:\\ollama-models`、`E:\\local-multimodal-models` 这些**绝对路径**上，
出过两次事故：

- 清理磁盘时把权重删了 → 代码找不到权重就去联网下载（国内连不上）→
  界面**一直显示"等待上线中"**，两周都没人发现；
- 系统的 Ollama 托盘版**无视环境变量**、只认自己数据库里的模型目录，
  升级后设置被重置 → 模型目录指向一个联接 → **模型全"消失"**（报 404 model not found）。

现在改成：**权重在包内用相对路径找**，Ollama 也是包内自带的、由启动器指定模型目录，
这类问题从根上没有了。

## 注意

- 需要联网吗？**完全不需要**（除了你自己要用的联网搜索功能）。
- 显存：对话模型约 19 GB + 绘图约 7 GB，12 GB 显存会部分走内存，属正常。
- 卸载 = 直接删掉这个目录（无注册表残留）。
"""


def write_bat(path, text):
    """批处理必须 GBK + CRLF。"""
    data = text.replace("\n", "\r\n").encode("gbk", errors="replace")
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def main():
    if not os.path.isdir(BUNDLE):
        print("✗ 包目录不存在: %s" % BUNDLE)
        return 1
    print("=" * 66)
    print("生成入口文件 -> %s" % BUNDLE)
    print("=" * 66)

    files = [
        ("启动.bat", write_bat(os.path.join(BUNDLE, "启动.bat"), START_BAT), "GBK"),
        ("创建快捷方式.bat",
         write_bat(os.path.join(BUNDLE, "创建快捷方式.bat"), SHORTCUT_BAT), "GBK"),
        ("检查自检.bat",
         write_bat(os.path.join(BUNDLE, "检查自检.bat"), SELFCHECK_BAT), "GBK"),
    ]
    with open(os.path.join(BUNDLE, "使用说明.md"), "w", encoding="utf-8") as f:
        f.write(README)
    files.append(("使用说明.md", len(README.encode("utf-8")), "UTF-8"))

    for name, n, enc in files:
        print("  %-18s %6d 字节  (%s)" % (name, n, enc))

    # 顺手验证批处理能跑（只读检查，不真启动应用）
    print()
    r = subprocess.run(["cmd", "/c", os.path.join(BUNDLE, "检查自检.bat")],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace", input="\r\n")
    out = (r.stdout or "").strip()
    print("--- 自检.bat 试跑输出 ---")
    for line in out.splitlines()[:20]:
        print("   " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
