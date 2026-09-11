@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)
echo.
echo Build completed.
echo The distributable ZIP is in the release folder.
echo Do NOT distribute only PMN-003_MONTAZH.exe; the _internal folder is required.
pause
