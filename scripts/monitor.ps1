# Opens the live activity monitor of the agent bus in its own console window. Closing it does not stop the bus.
#
#   powershell -File scripts/monitor.ps1 [-Port 8765] [-Quiet]
#
# -Quiet hides message reads, deliveries and session starts. Port: -Port, else $env:AGENTBUS_PORT, else 8765.
param([int]$Port = 0, [switch]$Quiet)
$ErrorActionPreference = 'Stop'

$package = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($Port -eq 0) { $Port = if ($env:AGENTBUS_PORT) { [int]$env:AGENTBUS_PORT } else { 8765 } }
$arguments = @('run', '--project', "`"$package`"", 'agentbus', '--port', $Port, 'watch')
if ($Quiet) { $arguments += '--quiet' }
Start-Process -FilePath 'uv' -ArgumentList $arguments | Out-Null
Write-Output "agentbus monitor opened (port $Port)"
