@echo off
setlocal
cd /d "%~dp0"
title PMN-003 MONTAZH
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto use_python
py -3 bootstrap.py
goto finished
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto missing
python bootstrap.py
goto finished
:missing
echo Python 3.10 or newer was not found. Python 3.12 is recommended.
echo Install Python from https://www.python.org/downloads/windows/
echo Then run START.cmd again.
pause
exit /b 1
:finished
if errorlevel 1 (
  echo.
  echo PMN-003 stopped with an error. Please take a screenshot of this window.
  pause
)
endlocal
