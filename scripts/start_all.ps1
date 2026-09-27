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
    if ($LASTEXITCODE -ne 0) { throw '无法读取本机数据目录配置，请检查 .env。' }
} finally { Pop-Location }
$RuntimeDir = Join-Path $StateDir 'data\runtime'
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'app\static_vite\index.html'))) {
    throw '请先在 frontend 目录运行 npm ci 和 npm run build。'
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
