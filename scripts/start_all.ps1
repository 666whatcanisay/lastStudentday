param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$ServiceUrl = "http://127.0.0.1:$Port"
if (-not (Test-Path -LiteralPath $PythonExe)) { throw '请先创建 .venv 并运行 scripts/install_dependencies.py。' }
Push-Location $ProjectDir
try {
    $StateDir = & $PythonExe -c 'from app.config import DATA_ROOT; print(DATA_ROOT)'
    # 必须把「python 根本没跑起来」和「python 跑了但 .env 配错」分开报。
    # $LASTEXITCODE 为 $null 说明进程压根没被执行（受限环境拦截 / .venv 不完整），
    # 此时笼统地说「请检查 .env」会把人引到完全错误的方向（实测被带偏过）。
    if ($null -eq $LASTEXITCODE) {
        throw "无法执行 $PythonExe（未返回退出码）。若处于受限环境（沙箱/安全软件拦截），请在普通终端手动运行本脚本；否则请检查 .venv 是否完整。"
    }
    if ($LASTEXITCODE -ne 0) { throw '无法读取本机数据目录配置，请检查 .env。' }
    if ([string]::IsNullOrWhiteSpace($StateDir)) { throw '未能取得本机数据目录（DATA_ROOT 为空），请检查 .env 中的 DOC_OCR_DATA_DIR。' }
} finally { Pop-Location }
$RuntimeDir = Join-Path $StateDir 'data\runtime'
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'app\static_vite\index.html'))) {
    throw '请先在 frontend 目录运行 npm ci 和 npm run build。'
}
# Recognize only a PID recorded by this project's data root, with the same
# creation identity, command line and listening port. A successful health
# response alone is not proof of ownership.
$PidFile = Join-Path $RuntimeDir 'server.pid'
if (Test-Path -LiteralPath $PidFile) {
    try {
        $Recorded = Get-Content -LiteralPath $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $RecordedWorkdir = [IO.Path]::GetFullPath([string]$Recorded.workdir).TrimEnd('\')
        $RecordedDataRoot = [IO.Path]::GetFullPath([string]$Recorded.data_root).TrimEnd('\')
        $ExpectedWorkdir = [IO.Path]::GetFullPath($ProjectDir).TrimEnd('\')
        $ExpectedDataRoot = [IO.Path]::GetFullPath($StateDir).TrimEnd('\')
        $RecordedPid = [int]$Recorded.pid
        $Live = Get-CimInstance Win32_Process -Filter "ProcessId=$RecordedPid" -ErrorAction Stop
        $LiveCreated = if ($Live) { $Live.CreationDate.ToUniversalTime().Ticks.ToString() } else { '' }
        $SameProject = $RecordedWorkdir -ieq $ExpectedWorkdir -and $RecordedDataRoot -ieq $ExpectedDataRoot
        $SameProcess = $Live -and $RecordedPid -eq [int]$Recorded.identity.pid -and `
            $LiveCreated -eq [string]$Recorded.identity.created -and `
            $Live.CommandLine -ceq [string]$Recorded.identity.command
        $ExpectedPort = "(?i)(?:^|\s)--port\s+$Port(?:\s|$)"
        $UsesPort = $Live -and $Live.CommandLine -match $ExpectedPort
    } catch {
        $SameProject = $false
        $SameProcess = $false
        $UsesPort = $false
    }
    if ($SameProject -and $SameProcess -and $UsesPort) {
        for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
            $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -eq '127.0.0.1' } | Select-Object -First 1
            if ($Listener) {
                if ($Listener.OwningProcess -ne $RecordedPid) {
                    throw "端口 $Port 已被其他进程占用，未打开页面，也未停止任何进程。"
                }
                try {
                    $Request = [System.Net.HttpWebRequest]::Create("$ServiceUrl/api/health")
                    $Request.Proxy = $null
                    $Request.Timeout = 1500
                    $Response = $Request.GetResponse()
                    $Response.Close()
                    Write-Host "本项目已经在运行：$ServiceUrl（未重复启动）"
                    if (-not $NoBrowser) { Start-Process $ServiceUrl }
                    exit 0
                } catch { }
            }
            Start-Sleep -Seconds 1
        }
        throw "已找到本项目进程 PID=$RecordedPid，但服务未就绪。请查看 $RuntimeDir\server.stderr.log；不要重复启动。"
    }
}
# Never infer process ownership from a successful health response. A different
# checkout/data directory may be using the same port. No automatic killing.
$Connection = New-Object System.Net.Sockets.TcpClient
try { $Connection.Connect('127.0.0.1', $Port); $Occupied = $true } catch { $Occupied = $false }
$Connection.Dispose()
if ($Occupied) {
    Write-Host "端口 $Port 已有服务。本次未启动或停止进程，请确认服务归属后访问 $ServiceUrl 或使用 -Port 更换端口。"
    exit 1
}
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$LaunchArgs = @('-m', 'uvicorn', 'app.main:app', '--app-dir', "`"$ProjectDir`"", '--host', '127.0.0.1', '--port', "$Port")
$ServiceProcess = Start-Process -FilePath $PythonExe -ArgumentList $LaunchArgs `
    -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $RuntimeDir 'server.stdout.log') `
    -RedirectStandardError (Join-Path $RuntimeDir 'server.stderr.log')
# The app itself records its actual PID/creation identity after acquiring the
# data-directory lock; the .venv launcher PID is not trusted as the server PID.
for ($Attempt = 0; $Attempt -lt 90; $Attempt++) {
    if ($ServiceProcess.HasExited) { throw "启动失败，请查看 $RuntimeDir\server.stderr.log" }
    try {
        $Request = [System.Net.HttpWebRequest]::Create("$ServiceUrl/api/health")
        $Request.Proxy = $null
        $Request.Timeout = 1500
        $Response = $Request.GetResponse()
        $Response.Close()
        Write-Host "服务已就绪：$ServiceUrl （仅本机，未开启远程访问）"
        if (-not $NoBrowser) { Start-Process $ServiceUrl }
        exit 0
    } catch { Start-Sleep -Seconds 1 }
}
throw "启动等待超时。请查看 $RuntimeDir\server.stderr.log；不要重复启动。"
