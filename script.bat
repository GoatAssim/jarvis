@echo off
setlocal

rem ---------------------------------------------------------------------
rem Thin launcher for install.py -- ALL the build/install/launch logic
rem lives there now. This file only picks a Python and hands over every
rem argument, so it is safe to drop in the startup apps:
rem
rem     script.bat                 full build + start the web server
rem     script.bat --serve-only    no build, just start the web server
rem     script.bat --no-venv       ignore any .venv, use plain Python
rem
rem Python is chosen the same way it always was: jarvis-cli\.venv, then a
rem root-level .venv, then plain "python" (then the "py" launcher). The
rem lines below are deliberately NOT parenthesized if-blocks: a ROOT such as
rem "C:\Program Files (x86)\..." has a ")" in it that would end the block early.
rem Set JARVIS_NO_PAUSE=1 to skip the "press a key" pause after an error.
rem ---------------------------------------------------------------------

set "ROOT=%~dp0"
set "PYEXE="
set "NOVENV="

for %%A in (%*) do if /i "%%~A"=="--no-venv" set "NOVENV=1"

if not defined NOVENV if exist "%ROOT%jarvis-cli\.venv\Scripts\python.exe" set PYEXE="%ROOT%jarvis-cli\.venv\Scripts\python.exe"
if not defined NOVENV if not defined PYEXE if exist "%ROOT%.venv\Scripts\python.exe" set PYEXE="%ROOT%.venv\Scripts\python.exe"
if not defined PYEXE python -c "import sys" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE py -3 -c "import sys" >nul 2>&1 && set "PYEXE=py -3"

if not defined PYEXE goto no_python

%PYEXE% "%ROOT%install.py" %*
set "RC=%errorlevel%"
if not "%RC%"=="0" echo. & echo install.py exited with code %RC%. & if not defined JARVIS_NO_PAUSE pause
exit /b %RC%

:no_python
echo ERROR: No working Python found. Install Python 3.8+ ^(and tick "Add to PATH"^), or create jarvis-cli\.venv.
if not defined JARVIS_NO_PAUSE pause
exit /b 1
