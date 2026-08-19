# De-elevated launcher for the Jarvis web server.
# Called from script.bat so Node/jarvis never inherit an admin token.
param(
    [Parameter(Mandatory = $true)]
    [string]$WebDir,
    [Parameter(Mandatory = $true)]
    [string]$JarvisExe
)

$WebDir = $WebDir.Trim('"')
$JarvisExe = $JarvisExe.Trim('"')

$starter = Join-Path $env:TEMP ("jarvis_web_" + [guid]::NewGuid().ToString("n") + ".bat")
@(
    '@echo off',
    "set `"JARVIS_BIN=$JarvisExe`"",
    "cd /d `"$WebDir`"",
    "call `"$WebDir\start-server.bat`""
) | Set-Content -Path $starter -Encoding ASCII

$shell = New-Object -ComObject Shell.Application
$shell.ShellExecute($starter, "", $WebDir, "", 1)

Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:4173"
