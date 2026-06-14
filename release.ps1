# release.ps1 — cut a release.
#
# Reads VERSION from claude_usage_tray.py, creates the matching vX.Y.Z tag, and
# pushes it. GitHub Actions (.github/workflows/release.yml) then builds the exe +
# installer and publishes the GitHub release.
#
# Workflow: bump VERSION in claude_usage_tray.py -> commit -> run .\release.ps1
#
#   powershell -ExecutionPolicy Bypass -File .\release.ps1

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "claude_usage_tray.py"
$ver = (Select-String -Path $src -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $ver) { Write-Host "Could not read VERSION from $src" -ForegroundColor Red; exit 1 }
$tag = "v$ver"

if (git status --porcelain) {
    Write-Host "Working tree not clean. Commit the VERSION bump before releasing." -ForegroundColor Red
    exit 1
}
if (git tag --list $tag) {
    Write-Host "Tag $tag already exists. Bump VERSION in claude_usage_tray.py first." -ForegroundColor Red
    exit 1
}

git tag $tag
git push origin $tag
Write-Host "Pushed $tag. GitHub Actions will build the exe + installer and publish the release:" -ForegroundColor Green
Write-Host "  https://github.com/stonelym/claude-usage/actions"
