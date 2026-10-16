# Stops the agent bus of a project. The state stays in <ProjectDir>/.agentbus/bus.db.
#
#   powershell -File scripts/stop.ps1 [-ProjectDir <dir>]
#
# Stops exactly the process recorded in <ProjectDir>/.agentbus/server.pid, and only if it is an agentbus process.
param([string]$ProjectDir = (Get-Location).Path)
$ErrorActionPreference = 'Stop'

$pidFile = Join-Path (Resolve-Path -LiteralPath $ProjectDir).Path '.agentbus\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "agentbus is not running for this project (no $pidFile)"
    exit 0
}
$serverPid = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid"
if (-not $process) {
    Remove-Item -LiteralPath $pidFile
    Write-Output "agentbus was not running (removed stale pid file, pid $serverPid)"
    exit 0
}
if ($process.CommandLine -notmatch 'agentbus') {
    Write-Output "pid $serverPid is not an agentbus process, not stopping it: $($process.CommandLine)"
    exit 1
}
Stop-Process -Id $serverPid -Force
Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
Write-Output "stopped agentbus (pid $serverPid)"
