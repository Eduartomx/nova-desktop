@echo off
setlocal
chcp 65001 >nul 2>&1
title Nova Updater - GitHub Native

set "ROOT=%~dp0"
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if not exist "%ROOT%\updater\update_runner.py" (
    echo [ERROR] Falta updater\update_runner.py
    echo Ejecutando updater directo como fallback...
    "%PY%" "%ROOT%\updater\nova_updater.py"
    set "RC=%ERRORLEVEL%"
    echo.
    pause
    exit /b %RC%
)

echo Nova se actualizara desde GitHub y se reiniciara automaticamente.
echo El resultado queda guardado en data\updater_logs y data\update_last.json.
echo.
"%PY%" "%ROOT%\updater\update_runner.py"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo El supervisor termino con codigo %RC%.
    echo Nova deberia haberse reiniciado igualmente para mostrar el error.
    pause
)
exit /b %RC%
