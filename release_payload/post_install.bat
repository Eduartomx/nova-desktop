@echo off
setlocal
set "ROOT=%~1"
if "%ROOT%"=="" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;$d=[Environment]::GetFolderPath('Desktop');" ^
  "$l=$ws.CreateShortcut((Join-Path $d 'Actualizar Nova.lnk'));" ^
  "$l.TargetPath=(Join-Path '%ROOT%' 'ACTUALIZAR_NOVA.cmd');$l.WorkingDirectory='%ROOT%';$l.Save()" >nul 2>&1
exit /b 0
