@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Double-click: uses local_out by default.
rem Drag a folder onto this file to use that folder instead.

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python not found. Install Python or create .venv in this folder.
    echo.
    pause
    exit /b 1
)

"%PY%" "%~dp0timestamps.py" %*

echo.
pause
