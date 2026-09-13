# Local Multimodal Assistant - host-side folder opener
#
# Why this exists:
#   The app runs inside a Linux container, so it cannot launch Windows Explorer.
#   This tiny agent runs ON THE HOST, watches a request file inside the mounted
#   data folder, and opens the folder for real. It also writes a heartbeat so
#   the app can tell whether the agent is alive.
#
# Lifecycle:
#   started hidden by  the one-click deploy script
#   stopped by         the stop / uninstall scripts
#
# Keep this file ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI when
# there is no BOM, so non-ASCII text would come out garbled.

param([string]$Root = "")

$ErrorActionPreference = "SilentlyContinue"

if (-not $Root) { $Root = $PSScriptRoot }

$DataDir  = Join-Path $Root "data"
$ReqFile  = Join-Path $DataDir ".open_folder_request"
$BeatFile = Join-Path $DataDir ".open_folder_agent"

# ---- single instance guard ------------------------------------------------
# Match ONLY a real agent invocation, i.e. a command line that actually passes
# this file via -File. A loose match like "contains open-folder-agent" would
# also hit any unrelated shell that merely mentions the name in its arguments
# (editors, other scripts, the stop script itself) and the agent would refuse
# to start. Always exclude our own process too.
$marker = '-File\s+.*open-folder-agent\.ps1'
$others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match $marker }
if ($others) { exit 0 }

$me = $PID

# utf-8 WITHOUT BOM: the stop script reads this file to find the PID, and a BOM
# would sit in front of "pid=" and break the match.
function Write-Beat([string]$state) {
    [IO.File]::WriteAllLines($BeatFile, @(
        "pid=$me",
        "$state $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    ), (New-Object Text.UTF8Encoding($false)))
}

Write-Beat "started"

$tick = 0
while ($true) {
    # heartbeat roughly every 5 seconds, so the app knows the agent is alive
    if (($tick % 6) -eq 0) { Write-Beat "alive" }
    $tick++

    if (Test-Path -LiteralPath $ReqFile) {
        $target = ""
        try { $target = (Get-Content -LiteralPath $ReqFile -Raw).Trim() } catch {}
        Remove-Item -LiteralPath $ReqFile -Force -ErrorAction SilentlyContinue

        if ($target) {
            # container path (/data/xxx) -> host path (<Root>\data\xxx)
            $p = $target.Replace('/', '\')
            if ($p.StartsWith('\')) { $p = Join-Path $Root $p.Substring(1) }

            if (Test-Path -LiteralPath $p) {
                $item = Get-Item -LiteralPath $p
                if ($item.PSIsContainer) {
                    Start-Process -FilePath "explorer.exe" -ArgumentList $item.FullName
                } else {
                    Start-Process -FilePath "explorer.exe" `
                        -ArgumentList "/select,`"$($item.FullName)`""
                }
            }
        }
    }

    Start-Sleep -Milliseconds 800
}
