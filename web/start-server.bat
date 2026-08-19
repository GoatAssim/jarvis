@echo off
setlocal
cd /d "%~dp0"

if defined JARVIS_BIN (
  echo Using jarvis: %JARVIS_BIN%
) else (
  echo JARVIS_BIN not set - server will auto-detect jarvis on PATH.
)

echo.
echo Jarvis web UI: http://127.0.0.1:4173
echo Close this window to stop the server.
echo.

node server.js
if errorlevel 1 pause
