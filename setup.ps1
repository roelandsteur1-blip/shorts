# One-time setup on Windows. Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "== Python virtual environment"
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip

Write-Host "== PyTorch with CUDA (for the RTX GPU)"
& $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126

Write-Host "== Other packages"
& $py -m pip install -r requirements.txt
& $py -m pip install -e .

Write-Host "== Font (Montserrat ExtraBold, open font licence)"
New-Item -ItemType Directory -Force fonts | Out-Null
$font = "fonts\Montserrat-ExtraBold.ttf"
if (-not (Test-Path $font)) {
  Invoke-WebRequest "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf" -OutFile $font
}

Write-Host "== Data folder (outside OneDrive)"
& $py -m shorts check
Write-Host ""
Write-Host "Done. Next: .\.venv\Scripts\python -m shorts ingest --per-term 3"
