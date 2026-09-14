@echo off
setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
set "JARVIS_CLI=%ROOT%jarvis-cli"
set "WEB_DIR=%ROOT%web"
rem Short path avoids "(x86)" breaking parenthesized if-blocks
set "INSTALL_DIR=C:\PROGRA~2\Utils\bin"
rem NOTE: the config directory (~\.jarvis) is NEVER renamed, on purpose --
rem it stays fixed for backwards compatibility no matter what persona/CLI
rem name is active. CLI_NAME_STATE just remembers the last-installed exe
rem name so a persona switch can clean up the old one; it has nothing to
rem do with where settings/history/etc. actually live.
set "CLI_NAME_STATE=%USERPROFILE%\.jarvis\cli_name.txt"
rem Prefer a project venv's Python if one exists (e.g. a 3.12 venv kept
rem around for packages like torch), so this script works correctly
rem whether or not the venv happens to be activated in the current shell.
rem Checks jarvis-cli\.venv first (where pyproject.toml lives, so it's
rem the natural place to create one) then falls back to a root-level
rem .venv, then to plain "python" if neither exists.
if exist "%JARVIS_CLI%\.venv\Scripts\python.exe" (
    set "PYEXE=%JARVIS_CLI%\.venv\Scripts\python.exe"
    set "VENV_SCRIPTS=%JARVIS_CLI%\.venv\Scripts"
) else if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PYEXE=%ROOT%.venv\Scripts\python.exe"
    set "VENV_SCRIPTS=%ROOT%.venv\Scripts"
) else (
    set "PYEXE=python"
    set "VENV_SCRIPTS="
)

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
rem %~2 is the CLI name the non-elevated parent already resolved/synced --
rem passed through so this elevated re-launch doesn't need to re-derive it
rem (and can't drift from what was actually just installed).
set "CLI_NAME=%~2"
if not defined CLI_NAME call :resolve_cli_name
set "INSTALLED_EXE=%INSTALL_DIR%\%CLI_NAME%.exe"
call :find_jarvis
if errorlevel 1 exit /b 1
echo [admin] Copying %CLI_NAME%.exe to Program Files...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
if /i "!JARVIS_EXE!"=="%INSTALLED_EXE%" (
    echo ERROR: Detected source and destination are the same file:
    echo   !JARVIS_EXE!
    echo Refusing to delete-then-copy from itself. This means find_jarvis
    echo couldn't locate a real freshly-built exe outside %INSTALL_DIR% --
    echo re-run the build without deleting the old install first.
    pause
    exit /b 1
)
call :cleanup_stale_exe
del /f /q "%INSTALLED_EXE%" 2>nul
copy /y "!JARVIS_EXE!" "%INSTALLED_EXE%"
if errorlevel 1 (
    echo ERROR: Failed to copy %CLI_NAME%.exe.
    pause
    exit /b 1
)
> "%CLI_NAME_STATE%" echo %CLI_NAME%
echo [admin] Done.
exit /b 0

:main
echo.
echo ==========================================
echo          JARVIS BUILD SCRIPT
echo ==========================================
echo.

cd /d "%JARVIS_CLI%"
if errorlevel 1 goto err_cd_cli

echo [1/5] Reading equipped persona...
call :sync_entry_point
if errorlevel 1 goto err_persona_sync
echo Persona's CLI command will be: %CLI_NAME%
echo (config still lives in %%USERPROFILE%%\.jarvis regardless of the above)
echo.

echo [1.5/5] Bumping build number...
set "BUMP_OUTPUT=%TEMP%\jarvis_build_num_%RANDOM%.txt"
"%PYEXE%" "%JARVIS_CLI%\build_tools\bump_build_version.py" > "%BUMP_OUTPUT%"
if errorlevel 1 (
    del /f /q "%BUMP_OUTPUT%" 2>nul
    echo WARNING: Could not bump build number ^(non-fatal^) -- version display may be stale.
) else (
    set /p "BUILD_NUM="<"%BUMP_OUTPUT%"
    del /f /q "%BUMP_OUTPUT%" 2>nul
    echo This build: #!BUILD_NUM!
)
echo.

echo [2/5] Installing CLI as "%CLI_NAME%"...
rem --force-reinstall is required here: plain `pip install .` only checks
rem name==version against what's already installed and silently no-ops if
rem that already matches -- it does NOT diff source file contents/timestamps.
rem Since this project's version string doesn't bump on every code change,
rem without this flag every run after the first would install NOTHING, and
rem you'd keep running a stale exe while every step above still reports
rem "success". --no-deps keeps this fast by skipping the (unchanged) dependency
rem tree -- if pyproject.toml's dependencies themselves changed, run a plain
rem "%PYEXE%" -m pip install . (no flags) once to pick those up too.
call "%PYEXE%" -m pip install . --force-reinstall --no-deps
if errorlevel 1 goto err_pip
echo.

echo [3/5] Locating %CLI_NAME%.exe...
call :find_jarvis
if errorlevel 1 goto err_no_exe
echo Found: !JARVIS_EXE!
echo.

echo [4/5] Updating Program Files copy...
set "INSTALLED_EXE=%INSTALL_DIR%\%CLI_NAME%.exe"
call :install_program_files
echo.

echo [5/5] Starting web server...
cd /d "%WEB_DIR%"
if errorlevel 1 goto err_cd_web
call npm install
if errorlevel 1 goto err_npm

call :launch_server
exit /b 0

rem --- Asks Python for the sanitized command name for whatever persona is
rem     currently equipped (~\.jarvis\ai_config.json's persona.assistant_name),
rem     AND rewrites jarvis-cli\pyproject.toml's [project.scripts] entry to
rem     match, so the upcoming `pip install .` bakes in the right exe name.
rem     Illegal characters (dots, spaces, punctuation - e.g. "L.O.L") are
rem     stripped by the Python side (jarvis\persona_name.py); this just
rem     wires that resolver into the build.
:sync_entry_point
set "CLI_NAME="
set "SYNC_OUTPUT=%TEMP%\jarvis_cli_name_%RANDOM%.txt"

"%PYEXE%" "%JARVIS_CLI%\build_tools\sync_entry_point.py" > "%SYNC_OUTPUT%"
if errorlevel 1 (
    del /f /q "%SYNC_OUTPUT%" 2>nul
    goto sync_entry_point_fail
)

set /p "CLI_NAME="<"%SYNC_OUTPUT%"
del /f /q "%SYNC_OUTPUT%" 2>nul

if not defined CLI_NAME goto sync_entry_point_fail
exit /b 0

:sync_entry_point_fail
echo ERROR: Could not resolve/sync the persona's CLI name.
echo Falling back to "jarvis" -- check that Python can import jarvis.ai_config.
set "CLI_NAME=jarvis"
exit /b 1
rem --- Same resolution as above, but read-only (no pyproject.toml rewrite) --
rem     used by the elevated admin-copy branch, which runs after the main
rem     flow already synced pyproject.toml and just needs the same answer
rem     again to know which exe filename to look for/copy.
:resolve_cli_name
set "CLI_NAME="
for /f "usebackq delims=" %%N in (`"%PYEXE%" -c "import sys; sys.path.insert(0, r'%JARVIS_CLI%'); from jarvis.persona_name import current_cli_name; print(current_cli_name())" 2^>nul`) do set "CLI_NAME=%%N"
if not defined CLI_NAME set "CLI_NAME=jarvis"
exit /b 0

:find_jarvis
set "JARVIS_EXE="
rem Check the project's own .venv first, if present -- this is where pip
rem installs the CLI when script.bat is run with a project venv active
rem (e.g. a Python 3.12 venv kept around for packages like torch that
rem don't yet support newer Pythons).
if defined VENV_SCRIPTS if exist "%VENV_SCRIPTS%\%CLI_NAME%.exe" set "JARVIS_EXE=%VENV_SCRIPTS%\%CLI_NAME%.exe" & goto find_jarvis_ok

rem No venv (or venv copy missing) -- ask the SAME python we just ran
rem `pip install .` with (whatever %PYEXE% resolved to, e.g. plain
rem "python" on PATH) where IT puts console-script exes. This logic lives
rem in build_tools\find_cli_exe.py (base scripts dir + the correct
rem per-user "nt_user" scheme dir, newest mtime wins if both exist) --
rem see that file's docstring for why it's not an inline one-liner here:
rem short version, an inline `python -c "..."` with its own parentheses
rem inside a `for /f` backtick command substitution is a real cmd.exe
rem parser landmine, and a plain hardcoded-location fallback is exactly
rem how an OLD exe keeps getting picked over the real freshly-built one.
set "PY_SCRIPTS="
set "FIND_EXE_OUTPUT=%TEMP%\jarvis_find_exe_%RANDOM%.txt"
"%PYEXE%" "%JARVIS_CLI%\build_tools\find_cli_exe.py" "%CLI_NAME%" > "%FIND_EXE_OUTPUT%" 2>nul
if not errorlevel 1 set /p "PY_SCRIPTS="<"%FIND_EXE_OUTPUT%"
del /f /q "%FIND_EXE_OUTPUT%" 2>nul
if defined PY_SCRIPTS if exist "%PY_SCRIPTS%" set "JARVIS_EXE=%PY_SCRIPTS%" & goto find_jarvis_ok

rem Last-resort fallback: whatever's on PATH. No more hardcoded
rem version-number guesses here -- those are exactly what kept matching
rem a stale exe instead of the real (possibly user-scoped) install.
rem
rem SAFETY: %INSTALL_DIR% (Program Files\...\bin) is itself on PATH once
rem installed once, since that's the point of installing there. That means
rem `where` can match the OLD exe already sitting at the copy DESTINATION,
rem which is about to be deleted and re-copied FROM -- i.e. we'd delete the
rem only copy of the file and then try to copy from nothing. Any PATH match
rem inside %INSTALL_DIR% is therefore never a valid "freshly built" source
rem and must be skipped.
where %CLI_NAME%.exe >nul 2>&1
if errorlevel 1 goto find_jarvis_fail
for /f "usebackq delims=" %%P in (`where %CLI_NAME%.exe 2^>nul`) do (
    if /i not "%%~dpP"=="%INSTALL_DIR%\" (
        set "JARVIS_EXE=%%P"
        goto find_jarvis_ok
    )
)
:find_jarvis_fail
echo ERROR: %CLI_NAME%.exe not found.
echo Tried venv (%VENV_SCRIPTS%), the base + user Python scripts dirs, and PATH.
echo (Skipped any PATH match inside %INSTALL_DIR% -- that's the old install target, not a new build.)
echo Run pip install from jarvis-cli first, or check that %PYEXE% is the python you expect.
exit /b 1
:find_jarvis_ok
exit /b 0

rem --- Deletes a previously-installed exe under a DIFFERENT persona name
rem     (e.g. friday.exe left behind after switching to Jarvis again), so
rem     Program Files\...\bin doesn't quietly accumulate one exe per
rem     persona you've ever equipped. Only touches the state file's exact
rem     recorded name, never guesses.
:cleanup_stale_exe
if not exist "%CLI_NAME_STATE%" exit /b 0
set "PREV_CLI_NAME="
for /f "usebackq delims=" %%L in ("%CLI_NAME_STATE%") do set "PREV_CLI_NAME=%%L"
if not defined PREV_CLI_NAME exit /b 0
if /i "%PREV_CLI_NAME%"=="%CLI_NAME%" exit /b 0
if exist "%INSTALL_DIR%\%PREV_CLI_NAME%.exe" (
    echo Removing previous persona's command: %PREV_CLI_NAME%.exe
    del /f /q "%INSTALL_DIR%\%PREV_CLI_NAME%.exe" 2>nul
)
exit /b 0

:install_program_files
net session >nul 2>&1
if errorlevel 1 goto install_needs_admin
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
if /i "!JARVIS_EXE!"=="%INSTALLED_EXE%" (
    echo WARNING: find_jarvis resolved to the existing Program Files copy
    echo itself ^(!JARVIS_EXE!^) instead of a freshly built exe -- skipping
    echo the copy rather than deleting the only remaining copy of it.
    exit /b 0
)
call :cleanup_stale_exe
del /f /q "%INSTALLED_EXE%" 2>nul
copy /y "!JARVIS_EXE!" "%INSTALLED_EXE%"
if errorlevel 1 echo WARNING: Program Files copy failed.
if not errorlevel 1 (
    echo Program Files copy updated.
    > "%CLI_NAME_STATE%" echo %CLI_NAME%
)
exit /b 0

:install_needs_admin
echo Requesting admin for Program Files copy only...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList 'admin-copy','%CLI_NAME%' -Wait"
if errorlevel 1 echo WARNING: Admin copy skipped - using user-local %CLI_NAME%.exe.
if not errorlevel 1 echo Program Files copy updated.
exit /b 0

:launch_server
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%launch-web.ps1" -WebDir "%WEB_DIR%" -JarvisExe "!JARVIS_EXE!"
echo.
echo Server window opened. JARVIS_BIN=!JARVIS_EXE! ^(command: %CLI_NAME%^)
echo.
exit /b 0

:err_cd_cli
echo ERROR: Could not enter jarvis-cli.
pause & exit /b 1
:err_persona_sync
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