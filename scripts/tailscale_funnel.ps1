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
  --bg 映射在 Tailscale 重启后仍可能恢复；用完必须显式 off。
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'status', 'off')]
    [string]$Action = 'status',
    [int]$Port = 8000,
    [ValidateSet('443')]
    [string]$HttpsPort = '443'
)

$ErrorActionPreference = 'Stop'
$Ts = 'C:\Program Files\Tailscale\tailscale.exe'
$ProjectRoot = Split-Path -Parent $PSScriptRoot

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
    if ($p.ExitCode -ne 0) {
        $detail = if ([string]::IsNullOrWhiteSpace($stderr)) { $stdout.Trim() } else { $stderr.Trim() }
        throw "Tailscale 命令失败（退出码 $($p.ExitCode)）：$detail"
    }
    return $stdout
}

function Get-ConfiguredOrigin {
    $value = $env:DOC_OCR_DEMO_PUBLIC_ORIGIN
    if ([string]::IsNullOrWhiteSpace($value)) {
        $envFile = Join-Path $ProjectRoot '.env'
        if (-not (Test-Path $envFile)) { throw "缺少 $envFile，无法核对公网域名" }
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*DOC_OCR_DEMO_PUBLIC_ORIGIN\s*=' } | Select-Object -Last 1
        if ($line) { $value = ($line -split '=', 2)[1] }
    }
    $value = ([string]$value).Trim().Trim('"').Trim("'").TrimEnd('/').ToLowerInvariant()
    if ($value -notmatch '^https://[a-z0-9.-]+\.ts\.net$') {
        throw '配置中缺少准确的 DOC_OCR_DEMO_PUBLIC_ORIGIN（https://设备名.tailnet.ts.net）'
    }
    return $value
}

function Get-JsonProperty {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-FunnelState {
    $json = [string](Invoke-Ts @('funnel', 'status', '--json'))
    if ([string]::IsNullOrWhiteSpace($json)) { throw 'Funnel 状态为空，无法确认映射' }
    try { $state = $json | ConvertFrom-Json }
    catch { throw "Funnel 状态不是有效 JSON：$($_.Exception.Message)" }
    if ($state -isnot [pscustomobject]) { throw 'Funnel 状态不是 JSON 对象，无法确认映射' }
    return $state
}

function Get-Mapping {
    param($State, [string]$Origin)
    $authority = ([Uri]$Origin).Host + ':' + $HttpsPort
    $site = Get-JsonProperty (Get-JsonProperty $State 'Web') $authority
    $root = Get-JsonProperty (Get-JsonProperty $site 'Handlers') '/'
    $tcp = Get-JsonProperty (Get-JsonProperty $State 'TCP') $HttpsPort
    $foreground443 = $false
    $foreground = Get-JsonProperty $State 'Foreground'
    if ($null -ne $foreground) {
        foreach ($entry in $foreground.PSObject.Properties) {
            $config = $entry.Value
            $fgTcp = Get-JsonProperty (Get-JsonProperty $config 'TCP') $HttpsPort
            $fgSite = Get-JsonProperty (Get-JsonProperty $config 'Web') $authority
            $fgAllow = Get-JsonProperty (Get-JsonProperty $config 'AllowFunnel') $authority
            if ($null -ne $fgTcp -or $null -ne $fgSite -or $null -ne $fgAllow) {
                $foreground443 = $true
                break
            }
        }
    }
    return [pscustomobject]@{
        Proxy = Get-JsonProperty $root 'Proxy'
        Allow = Get-JsonProperty (Get-JsonProperty $State 'AllowFunnel') $authority
        Https = Get-JsonProperty $tcp 'HTTPS'
        HasSite = $null -ne $site
        HasTcp = $null -ne $tcp
        Foreground443 = $foreground443
    }
}

function Test-ExpectedMapping {
    param($State, [string]$Origin)
    $mapping = Get-Mapping $State $Origin
    return ($mapping.Proxy -eq "http://127.0.0.1:$Port" -and
            $mapping.Allow -eq $true -and $mapping.Https -eq $true -and
            -not $mapping.Foreground443)
}

function Test-MappingAbsent {
    param($State, [string]$Origin)
    $mapping = Get-Mapping $State $Origin
    return (-not $mapping.HasSite -and -not $mapping.HasTcp -and
            $null -eq $mapping.Allow -and -not $mapping.Foreground443)
}

function Test-TsUp {
    param([string]$ExpectedOrigin)
    $st = [string](Invoke-Ts @('status', '--json')) | ConvertFrom-Json
    if ($st.BackendState -ne 'Running') {
        Write-Bad "Tailscale 尚未就绪（状态：$($st.BackendState)）。"
        Write-Info "处理：以管理员身份重启 Tailscale 服务，或打开 Tailscale 客户端登录，然后重跑本脚本。"
        Write-Info "  Restart-Service -Name Tailscale -Force   （需要管理员）"
        return $false
    }
    $dns = ([string]$st.Self.DNSName).TrimEnd('.').ToLowerInvariant()
    if (-not $st.TailscaleIPs -or [string]::IsNullOrWhiteSpace($dns)) {
        Write-Bad 'Tailscale 未取得 IP 或设备域名，不能开启 Funnel。'
        return $false
    }
    if ($ExpectedOrigin -ne "https://$dns") {
        Write-Bad "Tailscale 域名 https://$dns 与本机配置 $ExpectedOrigin 不一致。"
        return $false
    }
    Write-Ok "Tailscale 在线，设备域名：https://$dns"
    return $true
}

try {
    $origin = Get-ConfiguredOrigin
    switch ($Action) {

    'status' {
        $healthy = $true
        Write-Head "后端登录保护（http://127.0.0.1:$Port）"
        $code = Get-LocalStatus -P $Port
        if ($code -eq 401) {
            Write-Ok "未登录读取返回 401 —— 登录保护生效"
        } elseif ($code -eq 200) {
            Write-Bad "未登录读取返回 200 —— 登录保护没生效，绝对不能开 Funnel"
            $healthy = $false
        } else {
            Write-Bad "后端没响应（code=$code），请先启动服务"
            $healthy = $false
        }

        Write-Head "Tailscale 连接"
        if (-not (Test-TsUp -ExpectedOrigin $origin)) { $healthy = $false }

        Write-Head "Funnel 当前映射"
        $state = Get-FunnelState
        if (Test-ExpectedMapping $state $origin) {
            Write-Ok "$origin 正在公开映射到 http://127.0.0.1:$Port"
        } elseif (Test-MappingAbsent $state $origin) {
            Write-Info '当前没有本项目的 Funnel 映射。'
        } else {
            Write-Bad '443 端口有其他或不完整的映射，请人工检查后再操作。'
            $healthy = $false
        }
        if (-not $healthy) { exit 1 }
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
        if (-not (Test-TsUp -ExpectedOrigin $origin)) { exit 1 }

        $before = Get-FunnelState
        if (Test-ExpectedMapping $before $origin) {
            Write-Ok '准确的 Funnel 映射已存在，无需重复开启。'
            exit 0
        }
        if (-not (Test-MappingAbsent $before $origin)) {
            throw '443 端口已有其他或不完整的映射；为避免覆盖，已停止。'
        }

        Write-Head "3/3 建立公网映射（HTTPS $HttpsPort -> http://127.0.0.1:$Port）"
        $out = Invoke-Ts @('funnel', '--bg', "--https=$HttpsPort", "http://127.0.0.1:$Port")
        Write-Host $out
        $after = Get-FunnelState
        if (-not (Test-ExpectedMapping $after $origin)) {
            try { Invoke-Ts @('funnel', "--https=$HttpsPort", 'off') | Out-Null }
            catch { Write-Bad "自动撤销未确认映射失败：$($_.Exception.Message)" }
            throw 'Funnel 命令已执行，但未查到准确的 HTTPS 映射；已尝试撤销，仍须人工复核状态。'
        }
        Write-Ok "$origin 已映射到 http://127.0.0.1:$Port"
        Write-Info "关闭：scripts\tailscale_funnel.bat off"
    }

    'off' {
        Write-Head "关闭 Funnel 映射"
        if (-not (Test-TsUp -ExpectedOrigin $origin)) { exit 1 }
        $before = Get-FunnelState
        if (-not (Test-MappingAbsent $before $origin)) {
            if (-not (Test-ExpectedMapping $before $origin)) {
                throw '443 端口不是本项目的准确映射；为避免关闭其他服务，已停止。'
            }
            Write-Host (Invoke-Ts @('funnel', "--https=$HttpsPort", 'off'))
        }
        Write-Head "复核"
        $after = Get-FunnelState
        if (-not (Test-MappingAbsent $after $origin)) {
            throw '443 端口映射仍在，不能报告已关闭。'
        }
        Write-Ok "$origin 的 443 映射已移除。"
    }
}
} catch {
    Write-Bad $_.Exception.Message
    exit 1
}
