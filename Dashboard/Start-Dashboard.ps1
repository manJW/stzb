param([switch]$ShowBrowser)
$ErrorActionPreference = 'Stop'
$dashboardRoot = $PSScriptRoot
$dashboardUrl = 'http://127.0.0.1:8787'
$dashboardPython = 'C:\python310\python.exe'
try {
    $dashboardState = Invoke-RestMethod "$dashboardUrl/api/state" -TimeoutSec 3
    if ($dashboardState.app -ne 'stzb-local-observer') { throw 'The local port is occupied by another application.' }
} catch {
    $dashboardListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
    if ($dashboardListener) { throw 'Port 8787 is already in use. No application was stopped.' }
    if (-not (Test-Path -LiteralPath $dashboardPython -PathType Leaf)) { throw 'Python was not found at the configured location.' }
    $dashboardRuntime = Join-Path $dashboardRoot 'runtime'
    New-Item -ItemType Directory -Path $dashboardRuntime -Force | Out-Null
    $env:PYTHONUTF8 = '1'
    Start-Process -FilePath $dashboardPython -ArgumentList @('"' + (Join-Path $dashboardRoot 'server.py') + '"') -WorkingDirectory $dashboardRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dashboardRuntime 'server-out.log') -RedirectStandardError (Join-Path $dashboardRuntime 'server-error.log') | Out-Null
    $dashboardReady = $false
    for ($dashboardAttempt = 0; $dashboardAttempt -lt 20; $dashboardAttempt++) {
        Start-Sleep -Milliseconds 300
        try {
            $dashboardState = Invoke-RestMethod "$dashboardUrl/api/state" -TimeoutSec 2
            if ($dashboardState.app -eq 'stzb-local-observer') { $dashboardReady = $true; break }
        } catch {}
    }
    if (-not $dashboardReady) { throw 'Dashboard did not start. Check runtime/server-error.log.' }
}
if ($ShowBrowser) { Start-Process $dashboardUrl }
Write-Output $dashboardUrl
