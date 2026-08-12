@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Nova - Configurar conexion GitHub

set "ROOT=%~dp0"
set "GH="
where gh.exe >nul 2>&1
if not errorlevel 1 set "GH=gh.exe"
if not defined GH if exist "%ProgramFiles%\GitHub CLI\gh.exe" set "GH=%ProgramFiles%\GitHub CLI\gh.exe"

if not defined GH (
    echo Instalando GitHub CLI...
    winget install --id GitHub.cli -e --source winget --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo [ERROR] No pude instalar GitHub CLI.
        pause
        exit /b 1
    )
    if exist "%ProgramFiles%\GitHub CLI\gh.exe" set "GH=%ProgramFiles%\GitHub CLI\gh.exe"
)

if not defined GH (
    echo [ERROR] No encuentro gh.exe. Cierra esta ventana y vuelve a intentarlo.
    pause
    exit /b 1
)

"%GH%" auth status >nul 2>&1
if errorlevel 1 (
    echo Se abrira GitHub para iniciar sesion.
    "%GH%" auth login --hostname github.com --web --git-protocol https
    if errorlevel 1 exit /b 1
)

echo.
echo GitHub conectado. Probando Nova Updater...
if exist "%ROOT%\.venv\Scripts\python.exe" (
    "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\updater\nova_updater.py" --check
) else (
    python "%ROOT%\updater\nova_updater.py" --check
)
echo.
pause
