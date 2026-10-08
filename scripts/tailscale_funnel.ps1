<#
.SYNOPSIS
  Tailscale Funnel 开关：把本机 127.0.0.1:8000 临时映射成公网 HTTPS。

.DESCRIPTION
  只用 Funnel 暴露「一个」HTTPS 端口（443），应用本身仍然只绑 127.0.0.1，
  不监听局域网，也不改 0.0.0.0 —— 这是刻意的：
    · 无鉴权风险面最小；
    · tailscale serve/funnel 的反代目标本来就只支持 http://127.0.0.1。

  与 tailscale serve 的区别：serve 只对 tailnet 内可见，funnel 面向整个公网，
  所以开 funnel 之前必须先确认「未登录会被 401 拒绝」。

  用法（在项目根目录）：
    powershell -ExecutionPolicy Bypass -File scripts\tailscale_funnel.ps1 up
    powershell -ExecutionPolicy Bypass -File scripts\tailscale_funnel.ps1 status
    powershell -ExecutionPolicy Bypass -File scripts\tailscale_funnel.ps1 off

  也可以直接双击 scripts\tailscale_funnel.bat up

.NOTES
  本机重启后 Tailscale 与后端都不会自动带起，需要手动跑一次 up。
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'status', 'off')]
    [string]$Action = 'status',
    [int]$Port = 8000,
    [string]$HttpsPort = '443'
)

$ErrorActionPreference = 'Stop'
$Ts = 'C:\Program Files\Tailscale\tailscale.exe'

function Write-Head($text) { Write-Host ""; Write-Host "== $text ==" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Bad($text)  { Write-Host "  [STOP] $text" -ForegroundColor Red }
function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }

if (-not (Test-Path $Ts)) {
    Write-Bad "找不到 $Ts，请先安装 Tailscale 客户端。"
    exit 2
}

# 不用 curl.exe：本机沙箱下 curl.exe 能定位但起不来，$LASTEXITCODE 会是空的，
# 检查会「静默恒假」。这里用 .NET 直连，并显式清掉系统代理。
function Get-LocalStatus {
    param([int]$P)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$P/api/document/list")
        $req.Proxy = $null
        $req.Timeout = 8000
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return -1
    }
}

function Invoke-Ts {
    param([string[]]$TsArgs)
    # 不能用 `& $exe @args 2>&1 | Out-String`：PS 5.1 会报
    # CantActivateDocumentInPipeline（"无法在管道中间运行文档"），实测踩过。
    # 用 .NET Process 直接收 stdout/stderr，最稳。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Ts
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $quoted = @()
    foreach ($a in $TsArgs) {
        if ($a -match '[\s"]') { $quoted += ('"' + ($a -replace '"', '\"') + '"') } else { $quoted += $a }
    }
    $psi.Arguments = ($quoted -join ' ')
    $p = [System.Diagnostics.Process]::Start($psi)
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    if ([string]::IsNullOrWhiteSpace($stdout) -and -not [string]::IsNullOrWhiteSpace($stderr)) {
        return $stderr
    }
    if ($null -eq $stdout) { return '' }
    return $stdout
}

function Test-TsUp {
    $st = Invoke-Ts @('status')
    if ($st -match 'NoState' -or $st -match 'not logged in' -or $st -match 'Logged out') {
        Write-Bad "Tailscale 守护进程没有就绪（NoState）。"
        Write-Info "现象：服务在跑，但状态是 'Tailscale is starting. Please wait.'"
        Write-Info "处理：以管理员身份重启 Tailscale 服务，或打开 Tailscale 客户端登录，然后重跑本脚本。"
        Write-Info "  Restart-Service -Name Tailscale -Force   （需要管理员）"
        return $false
    }
    if ($st -match 'is starting') {
        Write-Bad "Tailscale 仍在启动，稍等 10~30 秒后重跑。"
        return $false
    }
    $ip = [string](Invoke-Ts @('ip', '-4'))
    $ip = $ip.Trim()
    if ([string]::IsNullOrWhiteSpace($ip)) {
        Write-Bad "Tailscale 没有分配到 IP，未连接。"
        return $false
    }
    Write-Ok "Tailscale 在线，本机 tailnet IP：$ip"
    return $true
}

switch ($Action) {

    'status' {
        Write-Head "后端登录保护（http://127.0.0.1:$Port）"
        $code = Get-LocalStatus -P $Port
        if ($code -eq 401) {
            Write-Ok "未登录读取返回 401 —— 登录保护生效"
        } elseif ($code -eq 200) {
            Write-Bad "未登录读取返回 200 —— 登录保护没生效，绝对不能开 Funnel"
        } else {
            Write-Bad "后端没响应（code=$code），请先启动服务"
        }

        Write-Head "Tailscale 连接"
        Test-TsUp | Out-Null

        Write-Head "Funnel 当前映射"
        Write-Host (Invoke-Ts @('funnel', 'status'))
        Write-Host (Invoke-Ts @('funnel', 'status', '--json'))
    }

    'up' {
        Write-Head "1/3 检查后端登录保护"
        $code = Get-LocalStatus -P $Port
        if ($code -ne 401) {
            Write-Bad "未登录读取返回 $code（期望 401）。"
            Write-Info "先把服务起起来并确认 .env 里 DOC_OCR_DEMO_MODE=1，再开 Funnel。"
            exit 1
        }
        Write-Ok "未登录读取返回 401，可以开 Funnel"

        Write-Head "2/3 检查 Tailscale 连接"
        if (-not (Test-TsUp)) { exit 1 }

        Write-Head "3/3 建立公网映射（HTTPS $HttpsPort -> http://127.0.0.1:$Port）"
        $out = Invoke-Ts @('funnel', '--bg', "--https=$HttpsPort", "http://127.0.0.1:$Port")
        Write-Host $out
        if ($out -match 'Funnel is not enabled' -or $out -match 'not enabled on your tailnet') {
            Write-Bad "你的 tailnet 还没开启 Funnel。"
            Write-Info "去 Tailscale 管理台 ACL 里加上 funnel 节点属性，再重跑本脚本："
            Write-Info '  "nodeAttrs": [ { "target": ["*"], "attr": ["funnel"] } ]'
            exit 1
        }
        if ($out -match 'HTTPS is not enabled' -or $out -match 'cert') {
            Write-Bad "tailnet 的 HTTPS 证书未开启，请到管理台 DNS 页面开启 HTTPS Certificates。"
            exit 1
        }

        Write-Head "结果"
        Write-Host (Invoke-Ts @('funnel', 'status'))
        Write-Info "公网地址应等于 .env 里 DOC_OCR_DEMO_PUBLIC_ORIGIN 配置的域名。"
        Write-Info "关闭：scripts\tailscale_funnel.bat off"
    }

    'off' {
        Write-Head "关闭 Funnel 映射"
        Write-Host (Invoke-Ts @('funnel', "--https=$HttpsPort", 'off'))
        Write-Head "复核"
        Write-Host (Invoke-Ts @('funnel', 'status', '--json'))
        Write-Info "上面应当是 {} 或没有 Foreground/AllowFunnel，表示公网已不可达。"
    }
}
