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

rem Real check, not just a version display: does `jarvis` on PATH (i.e.
rem whatever a plain invocation actually runs, which is not guaranteed to
rem be %JARVIS_BIN% above) match the source sitting in jarvis-cli/jarvis/
rem right now? Only runs if script.bat passed through what it needs
rem (CLI name, jarvis-cli path, a python to run it with) -- see
rem launch-web.ps1. Non-fatal: prints the result, never stops the server.
if defined JARVIS_CLI_NAME if defined JARVIS_CLI_DIR if defined JARVIS_PYEXE (
  "%JARVIS_PYEXE%" "%JARVIS_CLI_DIR%\build_tools\verify_exe.py" "%JARVIS_CLI_NAME%"
  echo.
)

echo.
echo Jarvis web UI: http://127.0.0.1:4173
echo Close this window to stop the server.
echo.

node server.js
if errorlevel 1 pause
