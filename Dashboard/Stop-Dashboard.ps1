$ErrorActionPreference = 'Stop'
$dashboardUrl = 'http://127.0.0.1:8787'
try { $dashboardState = Invoke-RestMethod "$dashboardUrl/api/state" -TimeoutSec 3 }
catch { Write-Output 'Dashboard is not running.'; return }
if ($dashboardState.app -ne 'stzb-local-observer') { throw 'This is a different local application. Nothing was stopped.' }
$dashboardService = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\service.json') -Raw | ConvertFrom-Json
if ($dashboardService.port -ne 8787) { throw 'Unexpected service port. Nothing was stopped.' }
Invoke-RestMethod "$dashboardUrl/api/stop" -Method Post -Headers @{'X-Local-Stop'=$dashboardService.stop_token} -TimeoutSec 10 | Out-Null
if (Get-Process -Id $dashboardService.pid -ErrorAction SilentlyContinue) {
    try { Wait-Process -Id $dashboardService.pid -Timeout 20 -ErrorAction Stop }
    catch { if (Get-Process -Id $dashboardService.pid -ErrorAction SilentlyContinue) { throw } }
}
Write-Output 'Dashboard and its capture process have been stopped.'
