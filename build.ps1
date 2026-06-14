# build.ps1 — build ClaudeUsage.exe and install it to %LOCALAPPDATA%\Programs\ClaudeUsage
#
# Usage: run from the folder containing claude_usage_tray.py
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
#
# After install, enable "Run at startup" from the tray menu — that creates
# the HKCU Run entry that Task Manager > Startup apps reads.

$ErrorActionPreference = "Stop"
$src  = Join-Path $PSScriptRoot "claude_usage_tray.py"
$dest = Join-Path $env:LOCALAPPDATA "Programs\ClaudeUsage"

if (-not (Test-Path $src)) {
    Write-Host "claude_usage_tray.py not found next to build.ps1" -ForegroundColor Red
    exit 1
}

Write-Host "[1/4] Installing build dependencies..." -ForegroundColor Cyan
# comtypes drives UI Automation, used to find the Win11 Widgets button so the
# taskbar badge can dodge it (see detect_taskbar_obstacles).
python -m pip install --quiet --upgrade pyinstaller pystray pillow requests comtypes

Write-Host "[2/4] Building ClaudeUsage.exe (takes a minute)..." -ForegroundColor Cyan
python -m PyInstaller --onefile --noconsole --name ClaudeUsage `
    --hidden-import pystray._win32 `
    --hidden-import comtypes `
    --distpath "$PSScriptRoot\dist" `
    --workpath "$PSScriptRoot\build" `
    --specpath "$PSScriptRoot" `
    $src

$exe = Join-Path $PSScriptRoot "dist\ClaudeUsage.exe"
if (-not (Test-Path $exe)) {
    Write-Host "Build failed - no exe produced. Scroll up for PyInstaller errors." -ForegroundColor Red
    exit 1
}

Write-Host "[3/4] Installing to $dest ..." -ForegroundColor Cyan
# Stop a running instance so the copy doesn't hit a file lock
Get-Process ClaudeUsage -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item $exe $dest -Force

# If a startup entry already exists (e.g. created while running as a script),
# repoint it at the installed exe so login launches the right thing.
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$existing = Get-ItemProperty -Path $runKey -Name "ClaudeUsage" -ErrorAction SilentlyContinue
if ($existing) {
    Set-ItemProperty -Path $runKey -Name "ClaudeUsage" -Value "`"$dest\ClaudeUsage.exe`""
    Write-Host "        Startup entry repointed at the installed exe." -ForegroundColor DarkGray
}

Write-Host "[4/4] Launching..." -ForegroundColor Cyan
Start-Process "$dest\ClaudeUsage.exe"

Write-Host ""
Write-Host "Done. Installed: $dest\ClaudeUsage.exe" -ForegroundColor Green
Write-Host "Right-click the tray icon -> 'Run at startup' to register it."
Write-Host "It will then appear in Task Manager > Startup apps with its own toggle."
