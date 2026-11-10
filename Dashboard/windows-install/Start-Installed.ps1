param([Parameter(Mandatory)][string]$Workspace, [switch]$Stop, [switch]$NoOpen)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$title = '率土战场看板'
$workspace = [IO.Path]::GetFullPath($Workspace)
$settingsPath = Join-Path $workspace 'install-settings.json'
$runtime = Join-Path $workspace 'runtime'
$servicePath = Join-Path $runtime 'service.json'

function Find-Dumpcap {
    $paths = @(
        (Join-Path $env:ProgramFiles 'Wireshark\dumpcap.exe')
        (Get-Command dumpcap.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    )
    foreach ($entry in (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
            Where-Object DisplayName -like 'Wireshark*')) {
        if ($entry.InstallLocation) {
            $paths += Join-Path $entry.InstallLocation 'dumpcap.exe'
        } elseif ($entry.UninstallString -match '^\s*"([^"]+\.exe)"') {
            $paths += Join-Path (Split-Path -Parent $Matches[1]) 'dumpcap.exe'
        }
    }
    $paths | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}

function Select-Executable([string]$Caption, [string]$Filename) {
    $dialog = New-Object Windows.Forms.OpenFileDialog
    try {
        $dialog.Title = $Caption
        $dialog.Filter = "$Filename|$Filename"
        if ($dialog.ShowDialog() -eq 'OK' -and [IO.Path]::GetFileName($dialog.FileName) -ieq $Filename) {
            return $dialog.FileName
        }
    } finally { $dialog.Dispose() }
}

function Initialize-Capture {
    $dumpcap = Find-Dumpcap
    if ($dumpcap -and (Get-Service -Name 'npcap' -ErrorAction SilentlyContinue)) { return $dumpcap }
    $helpText = '实时采集需要 Wireshark 和 Npcap。点「是」下载并打开官方安装程序，请保留安装 Npcap 的选项。已有自定义安装目录，可点「否」手动选择 dumpcap.exe。'
    $answer = [Windows.Forms.MessageBox]::Show($helpText, $title, 'YesNoCancel', 'Question')
    if ($answer -eq 'Cancel') { return }
    if ($answer -eq 'Yes') {
        $download = Join-Path $runtime 'Wireshark-4.6.9-x64.exe'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri 'https://2.na.dl.wireshark.org/win64/Wireshark-4.6.9-x64.exe' -OutFile $download -TimeoutSec 240
            $checksum = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
            $signature = Get-AuthenticodeSignature -LiteralPath $download
            if ($checksum -ne 'BF9B5CE8A89F244C376A9B1A946276EAA06463DDE3E33069A34D7F102F5878CF' -or
                $signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Wireshark Foundation(,|$)') {
                throw 'Wireshark 安装包未通过哈希和数字签名校验。请从官网 https://www.wireshark.org/download.html 安装。'
            }
            $result = Start-Process -FilePath $download -Wait -PassThru
            if ($result.ExitCode -ne 0) { throw 'Wireshark 安装未完成。安装后请再次打开看板。' }
        } finally {
            if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download -ErrorAction SilentlyContinue }
        }
        $dumpcap = Find-Dumpcap
    }
    if (!(Get-Service -Name 'npcap' -ErrorAction SilentlyContinue)) {
        throw '尚未检测到 Npcap。请在 Wireshark 安装程序中保留 Npcap 选项，完成后再次打开看板。'
    }
    if (!$dumpcap) { $dumpcap = Select-Executable '请选择 Wireshark 安装目录中的 dumpcap.exe' 'dumpcap.exe' }
    return $dumpcap
}

function Get-WorkspaceService {
    if (!(Test-Path -LiteralPath $servicePath -PathType Leaf)) { return }
    $record = Get-Content -LiteralPath $servicePath -Raw | ConvertFrom-Json
    if ($record.port -lt 1 -or $record.port -gt 65535 -or !(Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) { return }
    try {
        $status = Invoke-RestMethod "http://127.0.0.1:$($record.port)/api/state" -TimeoutSec 3
        if ($status.app -eq 'stzb-local-observer' -and $status.health.local_install) { return $record }
    } catch {}
}

$digest = [Security.Cryptography.SHA256]::Create()
$mutexKey = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToUpperInvariant()))).Replace('-', '')
$digest.Dispose()
$mutex = New-Object Threading.Mutex($false, "Local\STZB-Setup-$mutexKey")
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) { return }
    $service = Get-WorkspaceService
    if ($Stop) {
        if ($service) {
            Invoke-RestMethod "http://127.0.0.1:$($service.port)/api/stop" -Method Post -Headers @{'X-Local-Stop'=$service.stop_token} -TimeoutSec 10 | Out-Null
        }
        return
    }
    if ($service) {
        if (!$NoOpen) { Start-Process "http://127.0.0.1:$($service.port)/battlefield" }
        return
    }
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    if (!(Test-Path -LiteralPath $settingsPath)) {
        $game = Get-CimInstance Win32_Process -Filter "Name = 'stzb.exe'" -ErrorAction SilentlyContinue |
            Where-Object ExecutablePath | Select-Object -First 1 -ExpandProperty ExecutablePath
        if (!$game) { $game = Select-Executable '请选择官方 PC 版游戏目录中的 stzb.exe' 'stzb.exe' }
        if (!$game) { return }
        $dumpcap = Initialize-Capture
        if (!$dumpcap) { return }
        $settings = @{executable=(Resolve-Path -LiteralPath $game).ProviderPath; dumpcap=$dumpcap}
        $settings | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding UTF8
    }
    $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 8787)
    try { $listener.Start() }
    catch [Net.Sockets.SocketException] {
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
        $listener.Start()
    }
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    $dashboard = Join-Path $PSScriptRoot 'Dashboard'
    $entry = Join-Path $dashboard 'server.py'
    $python = Join-Path $PSScriptRoot 'Python\python.exe'
    $errors = Join-Path $runtime 'server-error.log'
    $launch = @{
        FilePath=$python
        ArgumentList=@('-X', 'utf8', '-B', ('"' + $entry + '"'), '--local-install', ('"' + $workspace + '"'), '--port', $port)
        WorkingDirectory=$dashboard
        WindowStyle='Hidden'
        PassThru=$true
        RedirectStandardError=$errors
        RedirectStandardOutput=(Join-Path $runtime 'server-out.log')
    }
    $process = Start-Process @launch
    for ($i = 0; $i -lt 35; $i++) {
        if ($process.HasExited) { break }
        $service = Get-WorkspaceService
        if ($service -and $service.pid -eq $process.Id) {
            if (!$NoOpen) { Start-Process "http://127.0.0.1:$port/battlefield" }
            return
        }
        Start-Sleep -Milliseconds 350
    }
    throw "本机看板启动失败。可在此处查看详情：$errors"
} catch {
    if ($NoOpen) { throw }
    [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, $title, 'OK', 'Error')
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
