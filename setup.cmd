@echo off
setlocal
title Memoria - one-time setup

echo ============================================================
echo    Memoria - one-time setup
echo ============================================================
echo.
echo This prepares Memoria's photo engine on your PC. It needs an
echo internet connection and a few minutes. You only do this once.
echo.
echo (Tip: run "setup.cmd -SkipML" to skip face grouping and
echo  semantic search for a much smaller, faster install.)
echo.

REM --- 1) Python must be installed and on PATH ---------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python was not found on this PC.
    echo.
    echo     1. Install Python 3.12 from https://python.org
    echo     2. During install, TICK "Add Python to PATH"
    echo     3. Then run this setup again.
    echo.
    pause
    exit /b 1
)

REM --- 2) Build the backend environment (venv + tools + ML deps) ---------
echo Setting up the photo engine...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server\setup.ps1" %*
if errorlevel 1 (
    echo.
    echo [X] Setup did not finish. Scroll up to see what failed, fix it,
    echo     and run this setup again.
    echo.
    pause
    exit /b 1
)

REM --- 3) Desktop + Start-Menu shortcuts to Memoria.exe -----------------
echo.
echo Creating shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$exe = Join-Path '%~dp0' 'Memoria.exe';" ^
  "$here = ('%~dp0').TrimEnd('\');" ^
  "$targets = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'));" ^
  "foreach ($dir in $targets) { $lnk = $ws.CreateShortcut((Join-Path $dir 'Memoria.lnk')); $lnk.TargetPath = $exe; $lnk.WorkingDirectory = $here; $lnk.Description = 'Memoria - your photos, on your machine'; $lnk.Save() }"

echo.
echo ============================================================
echo    All set. Open Memoria from the Desktop shortcut, or
echo    double-click Memoria.exe in this folder.
echo ============================================================
echo.
echo Keep this folder where it is -- Memoria.exe and the "server"
echo folder next to it must stay together for the app to work.
echo.
pause
