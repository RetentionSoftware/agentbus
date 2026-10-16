# Starts the agent bus for a project as a background process that outlives the terminal and every Claude session,
# then opens the live monitor in its own window (unless -NoMonitor).
#
#   powershell -File scripts/start.ps1 [-ProjectDir <dir>] [-Port 8765] [-ProjectName <name>] [-Protocol <path>] [-NoMonitor]
#
# The database, the logs and the server's pid file live in <ProjectDir>/.agentbus/ (default: the current directory).
# Runs the agentbus checkout this script belongs to, through uv. Port: -Port, else $env:AGENTBUS_PORT, else 8765.
param(
    [string]$ProjectDir = (Get-Location).Path,
    [int]$Port = 0,
    [string]$ProjectName = '',
    [string]$Protocol = '',
    [switch]$NoMonitor
)
$ErrorActionPreference = 'Stop'

$package = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
if ($Port -eq 0) { $Port = if ($env:AGENTBUS_PORT) { [int]$env:AGENTBUS_PORT } else { 8765 } }
$stateDir = Join-Path $ProjectDir '.agentbus'
New-Item -ItemType Directory -Force $stateDir | Out-Null

function Get-BusHealth {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 } catch { return $null }
}

function Open-Monitor {
    if (-not $NoMonitor) { & (Join-Path $PSScriptRoot 'monitor.ps1') -Port $Port }
}

# Quotes one argument for Start-Process, which joins its argument list with spaces.
function ConvertTo-Argument([string]$value) { '"' + ($value -replace '"', '\"') + '"' }

$health = Get-BusHealth
if ($health) {
    Write-Output "agentbus already running on port ${Port}: project '$($health.project)', db $($health.db)"
    Open-Monitor
    exit 0
}

$arguments = @('run', '--project', $package, 'agentbus', '--port', $Port, 'serve', '--db', (Join-Path $stateDir 'bus.db'))
if ($ProjectName) { $arguments += @('--project-name', $ProjectName) }
if ($Protocol) { $arguments += @('--protocol', $Protocol) }
$log = Join-Path $stateDir 'bus.log'
$errorLog = Join-Path $stateDir 'bus.err.log'
# cmd.exe does the redirection: Start-Process -RedirectStandardOutput would make the server inherit this script's
# own output handles, and a caller that reads them through a pipe (CI, `| Out-Null`) would wait for the server to exit.
$command = ((@('uv') + $arguments | ForEach-Object { ConvertTo-Argument "$_" }) -join ' ') +
    ' >' + (ConvertTo-Argument $log) + ' 2>' + (ConvertTo-Argument $errorLog)
Start-Process -FilePath $env:ComSpec -ArgumentList '/d', '/s', '/c', "`"$command`"" `
    -WorkingDirectory $ProjectDir -WindowStyle Hidden

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    $health = Get-BusHealth
    if ($health) {
        $serverPid = (Get-Content -LiteralPath (Join-Path $stateDir 'server.pid') -Raw).Trim()
        Write-Output "agentbus started: http://127.0.0.1:$Port/mcp, project '$($health.project)', pid $serverPid, logs in $stateDir"
        Open-Monitor
        exit 0
    }
}
Write-Output "agentbus did not start; see $errorLog"
exit 1
