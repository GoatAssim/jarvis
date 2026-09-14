# De-elevated launcher for the Jarvis web server.
# Called from script.bat so Node/jarvis never inherit an admin token.
param(
    [Parameter(Mandatory = $true)]
    [string]$WebDir,
    [Parameter(Mandatory = $true)]
    [string]$JarvisExe,
    # The three below are optional so this script still works if called
    # some other way without them (e.g. a future dev tool that only cares
    # about launching the server, not about the PATH-vs-source-hash
    # check) -- start-server.bat just skips that check when any of these
    # are blank, same as it already does for JARVIS_BIN.
    [string]$CliName = "",
    [string]$JarvisCliDir = "",
    [string]$PyExe = ""
)

$WebDir = $WebDir.Trim('"')
$JarvisExe = $JarvisExe.Trim('"')
$CliName = $CliName.Trim('"')
$JarvisCliDir = $JarvisCliDir.Trim('"')
$PyExe = $PyExe.Trim('"')

$starter = Join-Path $env:TEMP ("jarvis_web_" + [guid]::NewGuid().ToString("n") + ".bat")
@(
    '@echo off',
    "set `"JARVIS_BIN=$JarvisExe`"",
    "set `"JARVIS_CLI_NAME=$CliName`"",
    "set `"JARVIS_CLI_DIR=$JarvisCliDir`"",
    "set `"JARVIS_PYEXE=$PyExe`"",
    "cd /d `"$WebDir`"",
    "call `"$WebDir\start-server.bat`""
) | Set-Content -Path $starter -Encoding ASCII

$shell = New-Object -ComObject Shell.Application
$shell.ShellExecute($starter, "", $WebDir, "", 1)

Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:4173"
