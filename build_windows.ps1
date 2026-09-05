$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    & py -3.12 -c "import sys; print(sys.version)" | Out-Host
    $basePython = "py"
    $baseArgs = @("-3.12")
} else {
    $pythonExe = Get-Command python -ErrorAction Stop
    & python --version | Out-Host
    $basePython = "python"
    $baseArgs = @()
}

$venv = Join-Path $PSScriptRoot ".venv-build"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $basePython @baseArgs -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --disable-pip-version-check --upgrade pip
& $py -m pip install --disable-pip-version-check -r requirements-build.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name PMN-003_MONTAZH `
    --collect-all imageio_ffmpeg `
    app.py

if (-not (Test-Path "dist\PMN-003_MONTAZH\PMN-003_MONTAZH.exe")) {
    throw "PMN-003_MONTAZH.exe was not generated."
}

Write-Host "Built: dist\PMN-003_MONTAZH\PMN-003_MONTAZH.exe"
