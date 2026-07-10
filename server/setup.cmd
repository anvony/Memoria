@echo off
REM Double-clickable one-time setup for Memoria's backend.
REM Windows opens a bare .ps1 in Notepad, so this wrapper runs it properly
REM (bypassing the execution policy for this one script only) and keeps the
REM window open at the end so you can read the result.
REM
REM Pass -SkipML to skip the face/semantic-search models:  setup.cmd -SkipML

echo Setting up Memoria (this needs an internet connection)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*

echo.
echo Setup finished. You can close this window.
pause
