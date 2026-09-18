# 本地多模态助手 · 安装包完整性校验
#
# 用途：把安装包拷到目标机器之后，先跑一遍这个，确认没有漏文件。
#       （26GB 的包最容易出的问题就是"拷贝中断/漏了某个大文件"，
#         与其等到部署时报错，不如先花 10 秒自检。）
#
# 用法：双击「校验安装包.bat」
#      或在本目录打开 PowerShell：  .\校验安装包.ps1
#      加 -Full 会额外算大文件的 SHA256（慢，但能查出静默损坏）

param(
    [switch]$Full,
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"

# 目录分隔符（用 [char]92 表示，避免引号里的反斜杠被误解析）
$sep = [char]92
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

$script:okCount = 0
$script:badCount = 0

function Say($kind, $msg) {
    switch ($kind) {
        "ok"   { Write-Host ("  [OK]   " + $msg) -ForegroundColor Green; $script:okCount++ }
        "bad"  { Write-Host ("  [缺失] " + $msg) -ForegroundColor Red;    $script:badCount++ }
        "warn" { Write-Host ("  [注意] " + $msg) -ForegroundColor Yellow }
        "info" { Write-Host ("         " + $msg) -ForegroundColor Gray }
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host "        本地多模态助手 · 安装包完整性校验"
Write-Host "============================================================"
Write-Host ""

$mfPath = Join-Path $root "安装包清单.json"
if (-not (Test-Path $mfPath)) {
    Write-Host "  [错误] 找不到「安装包清单.json」" -ForegroundColor Red
    Write-Host "         这个文件是打包时生成的，必须和其它内容一起拷贝。" -ForegroundColor Red
    Write-Host ""
    if (-not $NoPause) { Read-Host "按回车退出" }
    exit 1
}

try {
    $mf = (Get-Content $mfPath -Raw -Encoding UTF8) | ConvertFrom-Json
} catch {
    Write-Host ("  [错误] 清单解析失败：" + $_.Exception.Message) -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车退出" }
    exit 1
}

Write-Host ("  清单生成时间：" + $mf.generated_at)
if ($Full) { Write-Host "  深度校验模式（含 SHA256，会比较慢）" -ForegroundColor Yellow }
Write-Host ""

# ---------------- 1. 必需文件 ----------------
Write-Host "【1】必需文件"
foreach ($it in $mf.required_files) {
    $p = $root + $sep + $it.path.Replace("/", $sep)
    if (Test-Path $p) {
        $len = (Get-Item $p).Length
        if ($len -eq $it.bytes) {
            Say "ok" ($it.path + "   (" + $len + " 字节)")
        } else {
            Say "bad" ($it.path + "   大小不符：实际 " + $len + " / 应为 " + $it.bytes)
        }
    } else {
        Say "bad" $it.path
    }
}
Write-Host ""

# ---------------- 2. 关键大文件 ----------------
Write-Host "【2】关键大文件（镜像与安装程序）"
foreach ($it in $mf.key_files) {
    $p = $root + $sep + $it.path.Replace("/", $sep)
    if (-not (Test-Path $p)) {
        Say "bad" ($it.path + "   整个文件都没有")
        continue
    }
    $len = (Get-Item $p).Length
    if ($len -ne $it.bytes) {
        Say "bad" ($it.path + "   大小不符：实际 " + [math]::Round($len/1MB,1) + " MB / 应为 " + [math]::Round($it.bytes/1MB,1) + " MB（拷贝可能中断）")
        continue
    }
    if ($Full) {
        Say "info" ("正在计算 SHA256：" + $it.path)
        $h = (Get-FileHash $p -Algorithm SHA256).Hash.ToLower()
        if ($h -eq $it.sha256) {
            Say "ok" ($it.path + "   SHA256 一致")
        } else {
            Say "bad" ($it.path + "   SHA256 不符！实际 " + $h.Substring(0,16) + " / 应为 " + $it.sha256.Substring(0,16))
        }
    } else {
        Say "ok" ($it.path + "   (" + [math]::Round($len/1MB,1) + " MB)")
    }
}
Write-Host ""

# ---------------- 3. 模型目录 ----------------
Write-Host "【3】模型目录（这是最容易被漏拷的部分）"
foreach ($d in $mf.dirs) {
    $base = $root + $sep + $d.path.Replace("/", $sep)
    if (-not (Test-Path $base)) {
        Say "bad" ($d.path + "   整个目录都没有")
        continue
    }
    $files = @(Get-ChildItem -Path $base -Recurse -File -ErrorAction SilentlyContinue)
    $sum = 0
    foreach ($f in $files) { $sum += $f.Length }

    $missing = @()
    foreach ($item in $d.list) {
        $rel = @($item)[0]
        $fp = $base + $sep + $rel.Replace("/", $sep)
        if (-not (Test-Path $fp)) { $missing += $rel }
    }

    if ($missing.Count -gt 0) {
        Say "bad" ($d.path + "   缺少 " + $missing.Count + " 个文件")
        foreach ($m in ($missing | Select-Object -First 5)) { Say "info" ("缺: " + $m) }
    } elseif (($files.Count -ne $d.files) -or ($sum -ne $d.bytes)) {
        Say "bad" ($d.path + "   文件数 " + $files.Count + "/" + $d.files + "，总计 " + [math]::Round($sum/1GB,2) + " GB / 应为 " + [math]::Round($d.bytes/1GB,2) + " GB")
    } else {
        Say "ok" ($d.path + "   " + $files.Count + " 个文件，" + [math]::Round($sum/1GB,2) + " GB")
    }
}
Write-Host ""

# ---------------- 4. 运行环境 ----------------
Write-Host "【4】运行环境"
$drive = $root.Substring(0,1)
try {
    $free = (Get-PSDrive -Name $drive -ErrorAction Stop).Free
    if ($free -gt 45GB) {
        Say "ok" ("磁盘剩余 " + [math]::Round($free/1GB,1) + " GB")
    } elseif ($free -gt 25GB) {
        Say "warn" ("磁盘只剩 " + [math]::Round($free/1GB,1) + " GB，勉强够（建议留 45GB 以上）")
    } else {
        Say "bad" ("磁盘只剩 " + [math]::Round($free/1GB,1) + " GB，不够用 —— 运行需要约 30GB 额外空间")
    }
} catch {
    Say "warn" "读不到磁盘剩余空间"
}

$dv = $null
try { $dv = (docker version --format "{{.Server.Version}}" 2>$null) } catch { $dv = $null }
if ($dv) {
    Say "ok" ("Docker 引擎可用（Server " + $dv + "）")
    try {
        $info = docker info 2>$null | Out-String
        if ($info -match "nvidia") { Say "ok" "Docker 已启用 NVIDIA 运行时（GPU 可用）" }
        else { Say "warn" "Docker 里没有 nvidia 运行时 —— 将走 CPU 模式，速度会慢很多" }
    } catch { }
} else {
    $ins = Join-Path $root "installers\DockerDesktopInstaller.exe"
    if (Test-Path $ins) {
        Say "warn" "这台机器还没装 Docker —— 没关系，包里有：双击 installers\DockerDesktopInstaller.exe"
    } else {
        Say "bad" "这台机器没有 Docker，包里也没有安装程序"
    }
}

$gpu = $null
try { $gpu = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null) } catch { $gpu = $null }
if ($gpu) { Say "ok" ("检测到显卡：" + ($gpu | Select-Object -First 1)) }
else { Say "warn" "没检测到 NVIDIA 显卡 —— 能跑，但速度会慢很多（建议用有独显的机器）" }
Write-Host ""

# ---------------- 总结 ----------------
Write-Host "============================================================"
if ($script:badCount -eq 0) {
    Write-Host ("  校验通过！共 " + $script:okCount + " 项正常。") -ForegroundColor Green
    Write-Host "  下一步：双击「一键部署.bat」" -ForegroundColor Green
} else {
    Write-Host ("  发现 " + $script:badCount + " 个问题（" + $script:okCount + " 项正常）") -ForegroundColor Red
    Write-Host "  上面标 [缺失] 的条目要重新拷贝，重点检查 images\ 和 models\ 两个目录。" -ForegroundColor Red
}
Write-Host "============================================================"
Write-Host ""

if (-not $NoPause) { Read-Host "按回车退出" }
