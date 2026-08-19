@echo off
setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
set "JARVIS_CLI=%ROOT%jarvis-cli"
set "WEB_DIR=%ROOT%web"
rem Short path avoids "(x86)" breaking parenthesized if-blocks
set "INSTALL_DIR=C:\PROGRA~2\Utils\bin"
set "INSTALLED_EXE=%INSTALL_DIR%\jarvis.exe"

if /i "%~1"=="admin-copy" goto admin_copy

rem --- Drop elevation if the whole script was "Run as administrator" ---
net session >nul 2>&1
if %errorlevel% equ 0 goto deelevate_self

goto main

:deelevate_self
echo.
echo NOTE: Elevated window detected. Re-launching as your normal user...
echo.
powershell -NoProfile -Command "$sh=New-Object -ComObject Shell.Application; $sh.ShellExecute('cmd.exe','/c \"\"\"%~f0\"\"\"','\"\"\"%ROOT%\"\"\"','',1)"
exit /b 0

:admin_copy
call :find_jarvis
if errorlevel 1 exit /b 1
echo [admin] Copying jarvis.exe to Program Files...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
del /f /q "%INSTALLED_EXE%" 2>nul
copy /y "!JARVIS_EXE!" "%INSTALLED_EXE%"
if errorlevel 1 (
    echo ERROR: Failed to copy jarvis.exe.
    pause
    exit /b 1
)
echo [admin] Done.
exit /b 0

:main
echo.
echo ==========================================
echo          JARVIS BUILD SCRIPT
echo ==========================================
echo.

echo [1/4] Installing Jarvis CLI...
cd /d "%JARVIS_CLI%"
if errorlevel 1 goto err_cd_cli
pip install .
if errorlevel 1 goto err_pip
echo.

echo [2/4] Locating jarvis.exe...
call :find_jarvis
if errorlevel 1 goto err_no_exe
echo Found: !JARVIS_EXE!
echo.

echo [3/4] Updating Program Files copy...
call :install_program_files
echo.

echo [4/4] Starting web server...
cd /d "%WEB_DIR%"
if errorlevel 1 goto err_cd_web
call npm install
if errorlevel 1 goto err_npm

call :launch_server
exit /b 0

:find_jarvis
set "JARVIS_EXE="
for %%P in (
    "%LOCALAPPDATA%\Python\pythoncore-3.14-64\Scripts\jarvis.exe"
    "%LOCALAPPDATA%\Python\Python314\Scripts\jarvis.exe"
    "%APPDATA%\Python\Python314\Scripts\jarvis.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python314\Scripts\jarvis.exe"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python313\Scripts\jarvis.exe"
) do if exist "%%~P" set "JARVIS_EXE=%%~P" & goto find_jarvis_ok
where jarvis.exe >nul 2>&1
if errorlevel 1 goto find_jarvis_fail
for /f "usebackq delims=" %%P in (`where jarvis.exe 2^>nul`) do (
    set "JARVIS_EXE=%%P"
    goto find_jarvis_ok
)
:find_jarvis_fail
echo ERROR: jarvis.exe not found. Run pip install from jarvis-cli first.
exit /b 1
:find_jarvis_ok
exit /b 0

:install_program_files
net session >nul 2>&1
if errorlevel 1 goto install_needs_admin
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
del /f /q "%INSTALLED_EXE%" 2>nul
copy /y "!JARVIS_EXE!" "%INSTALLED_EXE%"
if errorlevel 1 echo WARNING: Program Files copy failed.
if not errorlevel 1 echo Program Files copy updated.
exit /b 0

:install_needs_admin
echo Requesting admin for Program Files copy only...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList 'admin-copy' -Wait"
if errorlevel 1 echo WARNING: Admin copy skipped - using user-local jarvis.exe.
if not errorlevel 1 echo Program Files copy updated.
exit /b 0

:launch_server
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%launch-web.ps1" -WebDir "%WEB_DIR%" -JarvisExe "!JARVIS_EXE!"
echo.
echo Server window opened. JARVIS_BIN=!JARVIS_EXE!
echo.
exit /b 0

:err_cd_cli
echo ERROR: Could not enter jarvis-cli.
pause & exit /b 1
:err_pip
echo ERROR: pip install failed.
pause & exit /b 1
:err_no_exe
pause & exit /b 1
:err_cd_web
echo ERROR: Could not enter web folder.
pause & exit /b 1
:err_npm
echo ERROR: npm install failed.
pause & exit /b 1
