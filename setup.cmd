@echo off
setlocal
title Memoria - one-time setup

REM Thin launcher. All of the work, and all of the on-screen output, lives in
REM server\setup.ps1 -- the only thing that has to happen here is the Python
REM check, because without Python we can't say anything useful from PowerShell.
REM
REM Pass-through arguments: setup.cmd -ShowOutput  shows the raw command output
REM instead of the progress bar.

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [X] Python was not found on this PC.
    echo.
    echo       1. Install Python 3.12 from https://python.org
    echo       2. During install, TICK "Add python.exe to PATH"
    echo       3. Then run this setup again.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server\setup.ps1" %*
set RC=%ERRORLEVEL%

echo.
pause
exit /b %RC%
