# Installs everything for the Shorts pipeline in one go. Paste in PowerShell:
#   irm https://raw.githubusercontent.com/roelandsteur1-blip/shorts/main/install.ps1 | iex
$ErrorActionPreference = "Stop"
$target = "$env:USERPROFILE\OneDrive\Documenten\shorts\pipeline"

Write-Host "== 1/4 Python, Git, ffmpeg (winget)" -ForegroundColor Cyan
winget install --silent --accept-source-agreements --accept-package-agreements -e --id Python.Python.3.12
winget install --silent --accept-source-agreements --accept-package-agreements -e --id Git.Git
winget install --silent --accept-source-agreements --accept-package-agreements -e --id Gyan.FFmpeg
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

Write-Host "== 2/4 Claude Code (terminal)" -ForegroundColor Cyan
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  irm https://claude.ai/install.ps1 | iex
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host "== 3/4 Pipeline ophalen naar $target" -ForegroundColor Cyan
if (Test-Path "$target\.git") { git -C $target pull } else { git clone https://github.com/roelandsteur1-blip/shorts.git $target }

Write-Host "== 4/4 Python-pakketten (PyTorch is ruim 2 GB, dit duurt even)" -ForegroundColor Cyan
Set-Location $target
powershell -ExecutionPolicy Bypass -File setup.ps1

Write-Host ""
Write-Host "Klaar. Start 'claude' een keer om in te loggen als dat nog niet gebeurd is." -ForegroundColor Green
