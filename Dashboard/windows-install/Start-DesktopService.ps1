param([Parameter(Mandatory)][string]$Workspace,[switch]$NoOpen,[switch]$Upgrade,[switch]$Stop)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
$workspace=[IO.Path]::GetFullPath($Workspace)
$runtime=Join-Path $workspace 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$servicePath=Join-Path $runtime 'service.json'
$python=Join-Path $PSScriptRoot 'Python\python.exe'

function Get-LocalService {
    try {
        $record=Get-Content -LiteralPath $servicePath -Raw | ConvertFrom-Json
        if ($record.port -lt 1024 -or $record.port -gt 65535 -or !$record.instance_id) { return }
        if (!(Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) { return }
        $info=Invoke-RestMethod "http://127.0.0.1:$($record.port)/api/instance" -TimeoutSec 3
        if ($info.app -eq 'stzb-local-observer' -and $info.local_install -and $info.instance_id -eq $record.instance_id) { return $record }
    } catch {}
}

function Get-CaptureProgram {
    $paths=@((Join-Path $env:ProgramFiles 'Wireshark\dumpcap.exe'))
    $onPath=Get-Command dumpcap.exe -ErrorAction SilentlyContinue
    if ($onPath) { $paths+=$onPath.Source }
    foreach ($entry in (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object DisplayName -like 'Wireshark*')) {
        if ($entry.InstallLocation) { $paths+=Join-Path $entry.InstallLocation 'dumpcap.exe' }
        elseif ($entry.UninstallString -match '^\s*"([^"]+\.exe)"') { $paths+=Join-Path (Split-Path -Parent $Matches[1]) 'dumpcap.exe' }
    }
    $installedSettings=Join-Path $workspace 'install-settings.json'
    if (Test-Path -LiteralPath $installedSettings) { $paths+=(Get-Content -LiteralPath $installedSettings -Raw | ConvertFrom-Json).dumpcap }
    $paths | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}

function Initialize-Capture {
    $dumpcap=Get-CaptureProgram
    if ($dumpcap -and (Get-Service npcap -ErrorAction SilentlyContinue)) { return $dumpcap }
    $answer=[Windows.Forms.MessageBox]::Show('首次采集需要安装官方 Wireshark / Npcap。即将打开随包附带的官方安装程序，请保留 Npcap 选项，并自行确认许可和管理员提示。完成后会继续启动客户端。','率土战场','OKCancel','Information')
    if ($answer -ne 'OK') { throw '采集组件尚未安装。可稍后重新打开客户端继续。' }
    $installer=Join-Path $PSScriptRoot 'Prerequisites\Wireshark-4.6.9-x64.exe'
    $hash=(Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    $signature=Get-AuthenticodeSignature -LiteralPath $installer
    if ($hash -ne 'BF9B5CE8A89F244C376A9B1A946276EAA06463DDE3E33069A34D7F102F5878CF' -or $signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Wireshark Foundation(,|$)') { throw '官方采集组件未通过签名或哈希校验，已停止启动。' }
    $result=Start-Process -FilePath $installer -Wait -PassThru
    if ($result.ExitCode -notin @(0,3010)) { throw '采集组件安装未完成，请安装后重新打开客户端。' }
    $dumpcap=Get-CaptureProgram
    if (!$dumpcap -or !(Get-Service npcap -ErrorAction SilentlyContinue)) { throw '尚未发现采集驱动；如安装程序提示重启，请重启后打开客户端。' }
    return $dumpcap
}

$mutexHash=[Security.Cryptography.SHA256]::Create()
$mutexKey=[BitConverter]::ToString($mutexHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToUpperInvariant()))).Replace('-','')
$mutexHash.Dispose()
$mutex=[Threading.Mutex]::new($false,"Local\STZB-Desktop-$mutexKey")
$locked=$false
try {
    try { $locked=$mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked=$true }
    if (!$locked) { return }
    $service=Get-LocalService
    if ($Stop) {
        if ($service) { Invoke-RestMethod "http://127.0.0.1:$($service.port)/api/stop" -Method Post -Headers @{'X-Local-Stop'=$service.stop_token} -TimeoutSec 10 | Out-Null }
        return
    }
    if ($Upgrade -and $service) {
        $oldProcess=Get-Process -Id $service.pid -ErrorAction SilentlyContinue
        if ($oldProcess.Path -ne $python) {
            $installRoot=[regex]::Escape((Split-Path -Parent $PSScriptRoot))
            if ($oldProcess.Path -notmatch "^$installRoot\\app-[0-9A-F]{16}\\Python\\python\.exe$") { throw '旧服务不属于此安装目录，已停止替换。' }
            Invoke-RestMethod "http://127.0.0.1:$($service.port)/api/stop" -Method Post -Headers @{'X-Local-Stop'=$service.stop_token} -TimeoutSec 10 | Out-Null
            if (!$oldProcess.WaitForExit(30000)) { throw '旧服务仍在保存数据，请稍后再打开客户端。' }
            $service=$null
        }
    }
    if ($service) { return }
    $dumpcap=Initialize-Capture
    @{distribution_v2=$true;dumpcap=$dumpcap;executable='auto'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $workspace 'install-settings.json') -Encoding UTF8
    $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,8787)
    try { $listener.Start() } catch [Net.Sockets.SocketException] { $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start() }
    $port=$listener.LocalEndpoint.Port;$listener.Stop()
    $dashboard=Join-Path $PSScriptRoot 'Dashboard'
    $process=Start-Process -FilePath $python -ArgumentList @('-X','utf8','-B',('"'+(Join-Path $dashboard 'server.py')+'"'),'--local-install',('"'+$workspace+'"'),'--port',$port) -WorkingDirectory $dashboard -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $runtime 'server-error.log') -RedirectStandardOutput (Join-Path $runtime 'server-out.log')
    for ($attempt=0;$attempt -lt 240;$attempt++) {
        $process.Refresh()
        if ($process.HasExited) { throw ('本机服务启动未完成。请查看 '+(Join-Path $runtime 'server-error.log')) }
        $ready=Get-LocalService
        if ($ready -and $ready.pid -eq $process.Id) { return }
        Start-Sleep -Milliseconds 500
    }
    throw '身份读取或初始资料导入耗时较长，请稍后重新打开客户端。'
} catch {
    if ($NoOpen) { throw }
    [Windows.Forms.MessageBox]::Show($_.Exception.Message,'率土战场','OK','Error') | Out-Null
} finally {
    if ($locked) { $mutex.ReleaseMutex() }; $mutex.Dispose()
}
