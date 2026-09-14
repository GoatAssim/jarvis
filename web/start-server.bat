@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if defined JARVIS_BIN (
  set "JARVIS_VERSION=version unavailable"
  for /f "usebackq delims=" %%V in (`"%JARVIS_BIN%" version 2^>nul`) do set "JARVIS_VERSION=%%V"
  echo Using jarvis: %JARVIS_BIN% - !JARVIS_VERSION!
) else (
  echo JARVIS_BIN not set - server will auto-detect jarvis on PATH.
)

echo.
echo Jarvis web UI: http://127.0.0.1:4173
echo Close this window to stop the server.
echo.

node server.js
if errorlevel 1 pause
