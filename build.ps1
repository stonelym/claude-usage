# build.ps1 — build ClaudeUsage.exe and install it to %LOCALAPPDATA%\Programs\ClaudeUsage
#
# Usage: run from the folder containing claude_usage_tray.py
#   powershell -ExecutionPolicy Bypass -File .\build.ps1            # build + install locally
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -Publish   # also cut a GitHub release
#
# After install, enable "Run at startup" from the tray menu — that creates
# the HKCU Run entry that Task Manager > Startup apps reads.
#
# -Publish uploads dist\ClaudeUsage.exe and its .sha256 sidecar to a GitHub
# release tagged from the VERSION constant in claude_usage_tray.py, which is
# how installed copies discover and verify updates. Requires the gh CLI.
param([switch]$Publish)

$ErrorActionPreference = "Stop"
$src  = Join-Path $PSScriptRoot "claude_usage_tray.py"
$dest = Join-Path $env:LOCALAPPDATA "Programs\ClaudeUsage"
$repo = "stonelym/claude-usage"   # must match GITHUB_REPO in claude_usage_tray.py

if (-not (Test-Path $src)) {
    Write-Host "claude_usage_tray.py not found next to build.ps1" -ForegroundColor Red
    exit 1
}

# VERSION is the single source of truth; parse it out to form the release tag.
$ver = (Select-String -Path $src -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $ver) { Write-Host "Could not read VERSION from $src" -ForegroundColor Red; exit 1 }
$tag = "v$ver"

# Fail fast on publish preconditions, before a long build.
if ($Publish) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host "gh CLI not found; cannot -Publish." -ForegroundColor Red; exit 1
    }
    # `gh release view` writes "release not found" to stderr and exits non-zero
    # when the tag is absent (the good case). Under -EAP Stop that stderr becomes
    # a terminating NativeCommandError, so relax it for just this probe.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    gh release view $tag --repo $repo 1>$null 2>$null
    $tagExists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eap
    if ($tagExists) {
        Write-Host "Release $tag already exists on $repo. Bump VERSION first." -ForegroundColor Red
        exit 1
    }
}

Write-Host "[1/5] Installing build dependencies..." -ForegroundColor Cyan
# comtypes drives UI Automation, used to find the Win11 Widgets button so the
# taskbar badge can dodge it (see detect_taskbar_obstacles).
python -m pip install --quiet --upgrade pyinstaller pystray pillow requests comtypes

Write-Host "[2/5] Building ClaudeUsage.exe v$ver (takes a minute)..." -ForegroundColor Cyan
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

# Sidecar: the SHA-256 the installed app checks before swapping in an update.
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
Set-Content -Path "$exe.sha256" -Value $hash -NoNewline -Encoding ascii

Write-Host "[3/5] Installing to $dest ..." -ForegroundColor Cyan
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

Write-Host "[4/5] Launching..." -ForegroundColor Cyan
Start-Process "$dest\ClaudeUsage.exe"

if ($Publish) {
    Write-Host "[5/5] Publishing release $tag to $repo ..." -ForegroundColor Cyan
    gh release create $tag "$exe" "$exe.sha256" `
        --repo $repo --title $tag --notes "ClaudeUsage $tag"
    Write-Host "Published $tag. Installed copies will offer the update within ~a day." -ForegroundColor Green
} else {
    Write-Host "[5/5] Skipping publish (use -Publish to cut a release)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done. Installed: $dest\ClaudeUsage.exe (v$ver)" -ForegroundColor Green
Write-Host "Right-click the tray icon -> 'Run at startup' to register it."
Write-Host "It will then appear in Task Manager > Startup apps with its own toggle."
