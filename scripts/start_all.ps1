#Requires -Version 5.1
<#
  doc-ocr-system 一键启动
    1) 确保后端在 127.0.0.1:8000 运行
    2) 用 Tailscale Serve 把后端安全地暴露给手机

  入口是同目录的 start_all.bat（双击即可）。
  逻辑刻意放在 .ps1 而不是 .bat —— 因为 .bat 在本机的 agent 环境里无法执行，
  没法验证；.ps1 可以真跑一遍再交付。
#>
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Proj = 'C:\Users\27715\Desktop\doc-ocr-system\doc-ocr-system'
$Port = 8000

Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  doc-ocr-system 一键启动' -ForegroundColor Cyan
Write-Host "  1) 确保后端在 127.0.0.1:$Port 运行" -ForegroundColor Cyan
Write-Host '  2) 用 Tailscale Serve 安全地暴露给手机' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''

# 健康检查用纯 .NET 实现，**刻意不调 curl.exe**。
# 原因：本机 agent 沙箱里 curl.exe 起不来（实测 $LASTEXITCODE 为空、输出为空），
# 于是检查恒为 false —— 后端明明在跑却报「未运行」。
# 用 .NET 既没有这个依赖，也能被真正验证。
function Test-Backend {
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
        $req.Proxy = $null          # 本机有系统级 HTTP_PROXY，必须显式关掉
        $req.Timeout = 3000
        $req.ReadWriteTimeout = 3000
        $req.Method = 'GET'
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return ($code -eq 200)
    } catch {
        return $false
    }
}

Set-Location -LiteralPath $Proj -ErrorAction SilentlyContinue

Write-Host '[1/3] 检查后端 ...'
if (Test-Backend) {
    Write-Host '      [OK] 后端已就绪' -ForegroundColor Green
} else {
    Write-Host '      后端未运行，正在新窗口启动 ...' -ForegroundColor Yellow
    Start-Process -FilePath "$Proj\.venv\Scripts\python.exe" `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port" `
        -WorkingDirectory $Proj
    Write-Host '      等待就绪（最多 30 秒）...'
    $ok = $false
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 2
        if (Test-Backend) { $ok = $true; break }
    }
    if ($ok) { Write-Host '      [OK] 后端已就绪' -ForegroundColor Green }
    else { Write-Host '      [X] 30 秒内未就绪 —— 请看后端窗口里的报错' -ForegroundColor Red }
}

Write-Host ''
Write-Host '[2/3] 检查 Tailscale ...'
$tsExe = $null
$cmd = Get-Command tailscale -ErrorAction SilentlyContinue
if ($cmd) {
    $tsExe = $cmd.Source
} elseif (Test-Path 'C:\Program Files\Tailscale\tailscale.exe') {
    $tsExe = 'C:\Program Files\Tailscale\tailscale.exe'
}

if (-not $tsExe) {
    Write-Host '      [X] 未检测到 Tailscale，手机访问这一步跳过' -ForegroundColor Red
    Write-Host ''
    Write-Host '          请先完成：'
    Write-Host '           1. 双击安装 C:\Users\27715\Downloads\tailscale-setup-1.102.4-amd64.msi'
    Write-Host '           2. 托盘图标右键 -> Log in'
    Write-Host '           3. 打开 https://login.tailscale.com/admin/dns'
    Write-Host '              开启 MagicDNS 与 HTTPS Certificates'
    Write-Host '          详见 WINDOWS_PHONE_ACCESS.md'
    return
}

Write-Host "      使用 $tsExe"
Write-Host '      正在配置 Serve ...'
& $tsExe serve --bg $Port
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '      [!] Serve 配置失败。最常见原因是控制台没开 HTTPS Certificates。' -ForegroundColor Yellow
    Write-Host "          可先用明文 HTTP 顶上： tailscale serve --bg --http=8080 $Port"
    return
}

Write-Host ''
Write-Host '[3/3] 手机浏览器打开下面这个地址：' -ForegroundColor Cyan
Write-Host ''
& $tsExe serve status
