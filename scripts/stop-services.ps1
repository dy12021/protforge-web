# Stops the three services, by listening port. Keep pure ASCII (cp936 console).
# Do NOT match on the command line: both server\app.py variants share one
# path and the role is only a bare argument. Matching the path would stop
# public and internal together, and a stale process keeps the port so the
# next start silently serves the previous build.
$ErrorActionPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format s

foreach ($port in 8473, 8474, 8475) {
    $conn = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($conn.Count -eq 0) {
        Add-Content -Path (Join-Path $logs 'stop-services.log') -Value "$stamp  $port already free"
        continue
    }
    foreach ($c in $conn) {
        Stop-Process -Id $c.OwningProcess -Force
        Add-Content -Path (Join-Path $logs 'stop-services.log') -Value "$stamp  $port stopped pid=$($c.OwningProcess)"
    }
}

Start-Sleep -Seconds 2
foreach ($port in 8473, 8474, 8475) {
    $still = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    Add-Content -Path (Join-Path $logs 'stop-services.log') `
        -Value "$stamp  $port " + $(if ($still.Count -eq 0) { 'free' } else { 'STILL HELD' })
}
