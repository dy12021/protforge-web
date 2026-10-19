# Stops the three services: by listening port AND by command line.
#
# Why there are two passes
# ------------------------
# Matching on the listening port alone misses one case: a process that runs
# our entry point but no longer holds a port. The port was taken by someone
# else, it never got to bind, or it is a leftover child. Such a process
# survives the stop, and then the next start sees "port already busy",
# skips starting, and the OLD build keeps serving the UI. The log line says
# "already listening", so nothing looks wrong.
#
# That is exactly the failure this script was written to prevent, so the
# second pass sweeps the command line as well. The "is this one of ours"
# rule lives in scripts\select_service_pids.py, as a testable pure function
# (tests\test_service_pids.py).
#
# Do NOT match the path loosely. This machine also runs other projects
# (media-center-pms, creative-panel, MaiBot) and one of them runs a bare
# "python app.py". Killing that would be wrong, so the rule requires:
#   - a python executable,
#   - an argument that IS the entry point (server\app.py, admin\app_admin.py),
#   - matched segment by segment (myserver\app.py is not server\app.py).
#
# Everything killed here is written to logs\stop-services.log with its full
# command line, so an unexpected kill is traceable.
#
# Keep this file pure ASCII (cp936 console).
$ErrorActionPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format s
$log = Join-Path $logs 'stop-services.log'

# ---- 1. by listening port -------------------------------------------------
# The port pass stays first on purpose: the process that HOLDS the port is
# the one that would keep serving the old build, whatever started it.
$stopped = @{}
foreach ($port in 8473, 8474, 8475) {
    $conn = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($conn.Count -eq 0) {
        Add-Content -Path $log -Value "$stamp  $port already free"
        continue
    }
    foreach ($c in $conn) {
        Stop-Process -Id $c.OwningProcess -Force
        $stopped[[int]$c.OwningProcess] = $true
        Add-Content -Path $log -Value "$stamp  $port stopped pid=$($c.OwningProcess)"
    }
}

# ---- 2. by command line (the leftovers that hold no port) -----------------
$py = 'C:\ProgramData\anaconda3\python.exe'
if (-not (Test-Path $py)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (Test-Path $py) {
    $rows = @()
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)) {
        $rows += @{ pid = [int]$p.ProcessId; name = $p.Name; cmdline = $p.CommandLine }
    }
    if ($rows.Count -gt 0) {
        # The command line may hold Chinese, so the JSON goes over a pipe that
        # is explicitly UTF-8: the console here is cp936 and the default
        # $OutputEncoding would garble it. No temp file involved.
        $json = ConvertTo-Json -InputObject $rows -Compress -Depth 3
        $prevEnc = $OutputEncoding
        # UTF8Encoding **without BOM** on purpose: a BOM at the head of the
        # JSON breaks the parser. (The helper also tolerates one.)
        $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $pids = @($json | & $py (Join-Path $root 'scripts\select_service_pids.py') 2>$null)
        $OutputEncoding = $prevEnc
        foreach ($pidText in $pids) {
            $target = 0
            if (-not [int]::TryParse([string]$pidText, [ref]$target)) { continue }
            if ($stopped.ContainsKey($target)) { continue }
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $target" -ErrorAction SilentlyContinue
            $shown = 'gone'
            if ($proc -and $proc.CommandLine) { $shown = $proc.CommandLine }
            Stop-Process -Id $target -Force -ErrorAction SilentlyContinue
            Add-Content -Path $log -Value "$stamp  orphan stopped pid=$target cmd=$shown"
        }
    }
}

# ---- 3. report ------------------------------------------------------------
Start-Sleep -Seconds 2
foreach ($port in 8473, 8474, 8475) {
    $still = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    Add-Content -Path $log `
        -Value "$stamp  $port " + $(if ($still.Count -eq 0) { 'free' } else { 'STILL HELD' })
}
