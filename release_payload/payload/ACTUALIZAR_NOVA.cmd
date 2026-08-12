@echo off
setlocal
chcp 65001 >nul 2>&1
title Nova Updater - GitHub

set "ROOT=%~dp0"
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if not "%~1"=="" (
    "%PY%" "%ROOT%\updater\nova_updater.py" "%~f1"
) else (
    "%PY%" "%ROOT%\updater\nova_updater.py"
)

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo El actualizador termino con codigo %RC%.
pause
exit /b %RC%
