# Starts the three automation-platform services, headless.
# Used by start.bat's sibling (scripts\start-services.ps1) and by the
# "protforge-web-services" logon task. Keep this file pure ASCII: the
# Windows console default codepage is cp936 and non-ASCII bytes in a
# comment turn into mojibake in the task history.
$ErrorActionPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# ---- optional .env (same keys start.bat understands) ------------------------
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
        if ($k) { Set-Item -Path "env:$k" -Value $v }
    }
}

# ---- python ----------------------------------------------------------------
$py = 'C:\ProgramData\anaconda3\python.exe'
if (-not (Test-Path $py)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py -or -not (Test-Path $py)) {
    Add-Content -Path (Join-Path $logs 'start-services.log') `
        -Value "$(Get-Date -Format s)  python not found"
    exit 1
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
if (-not $env:PROTFORGE_WEB_WORKERS) { $env:PROTFORGE_WEB_WORKERS = '1' }
# PROTFORGE_WEB_DEV_LOGIN is deliberately never set here: auth stays on.

$svcs = @(
    @{ name = 'public';   port = 8473; argv = @('server\app.py', 'public') },
    @{ name = 'internal'; port = 8475; argv = @('server\app.py', 'internal') },
    @{ name = 'admin';    port = 8474; argv = @('admin\app_admin.py') }
)

$stamp = Get-Date -Format s
foreach ($s in $svcs) {
    # Idempotent by *listening port*, not by process name: re-running this
    # script (or a second logon) must not spawn a duplicate that then fails
    # to bind and silently leaves an old build serving the UI.
    $busy = @(Get-NetTCPConnection -State Listen -LocalPort $s.port -ErrorAction SilentlyContinue)
    if ($busy.Count -gt 0) {
        Add-Content -Path (Join-Path $logs 'start-services.log') -Value "$stamp  $($s.name) :$($s.port) already listening"
        continue
    }
    Start-Process $py -ArgumentList $s.argv -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$($s.name).log") `
        -RedirectStandardError (Join-Path $logs "$($s.name).err.log") | Out-Null
    Add-Content -Path (Join-Path $logs 'start-services.log') -Value "$stamp  $($s.name) :$($s.port) started"
}

# uvicorn needs a moment to bind; report what actually came up.
Start-Sleep -Seconds 6
foreach ($s in $svcs) {
    $up = @(Get-NetTCPConnection -State Listen -LocalPort $s.port -ErrorAction SilentlyContinue)
    if ($up.Count -eq 0) {
        Add-Content -Path (Join-Path $logs 'start-services.log') -Value "$stamp  $($s.name) :$($s.port) FAILED to listen"
    }
}
