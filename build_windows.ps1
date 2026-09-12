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
& $py -m pip install --disable-pip-version-check -r requirements-release-build.txt

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

& $py -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name PMN-003_MONTAZH `
    --exclude-module imageio_ffmpeg `
    app.py

if (-not (Test-Path "dist\PMN-003_MONTAZH\PMN-003_MONTAZH.exe")) {
    throw "PMN-003_MONTAZH.exe was not generated."
}

# Add end-user documentation next to the executable.  The whole folder is the
# distributable application; do not distribute the EXE by itself.
Copy-Item -Force "RELEASE_README.txt" "dist\PMN-003_MONTAZH\README.txt"
Copy-Item -Force "THIRD_PARTY_NOTICES.txt" "dist\PMN-003_MONTAZH\THIRD_PARTY_NOTICES.txt"

# Preserve third-party license texts from the exact wheels used for this build.
$licenseDir = "dist\PMN-003_MONTAZH\licenses"
New-Item -ItemType Directory -Force -Path $licenseDir | Out-Null
$licenseCollector = @'
from importlib.metadata import distribution
from pathlib import Path
import shutil

dest = Path(r"dist\PMN-003_MONTAZH\licenses")
for package, label in (("opencv-python-headless", "OpenCV"), ("Pillow", "Pillow")):
    dist = distribution(package)
    copied = 0
    for item in dist.files or []:
        text = str(item).replace("\\", "/")
        leaf = Path(text).name
        if ".dist-info/" not in text.lower():
            continue
        if not any(word in leaf.lower() for word in ("license", "copying", "notice")):
            continue
        source = Path(dist.locate_file(item))
        if source.is_file():
            shutil.copy2(source, dest / f"{label}_{leaf}")
            copied += 1
    if copied == 0:
        raise SystemExit(f"No license metadata found for {package}")
'@
$licenseCollector | & $py -
if ($LASTEXITCODE -ne 0) { throw "Failed to collect third-party license files." }

# Create a ready-to-upload ZIP for GitHub Releases.
$version = (& $py -c "import engine; print(engine.VERSION)").Trim()
$releaseDir = Join-Path $PSScriptRoot "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$releaseZip = Join-Path $releaseDir ("PMN-003_MONTAZH_v{0}_Windows_x64.zip" -f $version)
if (Test-Path $releaseZip) { Remove-Item -Force $releaseZip }
Compress-Archive -Path "dist\PMN-003_MONTAZH" -DestinationPath $releaseZip -CompressionLevel Optimal

Write-Host "Built:   dist\PMN-003_MONTAZH\PMN-003_MONTAZH.exe"
Write-Host "Release: $releaseZip"
Write-Host "Distribute the ZIP, not the EXE alone."
