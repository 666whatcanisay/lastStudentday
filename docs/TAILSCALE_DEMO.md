# Tailscale Funnel 公网访问操作卡

目标：在**别的电脑（不装 Tailscale）**上打开一个 HTTPS 链接、输入一个口令，就能完整使用本系统。

## 前提与边界

- 应用始终只监听 `127.0.0.1:8000`，**不改 `0.0.0.0`**。原因：`tailscale serve/funnel` 的反代目标本来就只支持 `http://127.0.0.1`；绑 `0.0.0.0` 既没必要，也会把无鉴权 API 暴露到局域网。
- **统一口令**：只有一个口令，登录后即完整管理员权限。没有第二个管理员口令，没有账号体系。口令以 pbkdf2_sha256（600000 次）加盐哈希存在**被 gitignore 的 `.env`** 里，明文不落盘、不进仓库。
- 拿到链接和口令的人可查看、修改、导出当前项目中的全部正常文档；所有访客共用同一套数据，**无法区分操作人**。
- Funnel 只支持 HTTPS 公网端口 **443 / 8443 / 10000**，有不可调的带宽限制。

## 一次性准备（只做一次）

1. 安装 Tailscale Windows 客户端并登录你的 tailnet；到管理台 **DNS** 页面确认 **MagicDNS** 与 **HTTPS Certificates** 已开启，记下准确域名 `设备名.tailnet.ts.net`。
2. 到管理台 **ACL** 给节点加上 funnel 属性（否则 `funnel` 会报 "Funnel is not enabled"）：

   ```json
   "nodeAttrs": [ { "target": ["*"], "attr": ["funnel"] } ]
   ```

3. 配置域名与口令（口令由你指定，脚本不回显明文）：

   ```powershell
   .\.venv\Scripts\python.exe scripts\setup_demo_auth.py --origin "https://设备名.tailnet.ts.net" --password "你的口令"
   ```

   其他子命令：`--status`（看当前配置）· `--set-password "新口令"`（只轮换口令）· `--disable`（移除演示配置，回到无口令本机模式）。

   当前本机配置可用 `--status` 复核，应为：

   ```
   登录保护：已启用
   公网域名：https://wandertheearth.tail3e9b7d.ts.net
   模式：单口令（登录即完整管理员）
   ```

## 每次使用（三步）

```powershell
# 1) 启动后端（演示模式；改过 .env 必须重启才生效）
scripts\start_all.bat

# 2) 确认登录保护真的生效（未登录必须 401，返回 200 就绝不能开 Funnel）
scripts\tailscale_funnel.bat status

# 3) 开公网映射（HTTPS 443 -> http://127.0.0.1:8000）
scripts\tailscale_funnel.bat up
```

`status` 在登录保护失效、Tailscale 未就绪或命令失败时返回非零退出码；映射尚未开启但其余检查正常时可返回零。`up` 会先检查未登录 401、在线状态及设备域名，再核对 443 端口没有冲突；执行后还会确认状态中的 HTTPS 域名、公开权限与反代目标均准确，不能确认就返回失败。

关闭：

```powershell
scripts\tailscale_funnel.bat off
```

`up` 用的是 `--bg`（后台保持），所以关掉终端映射仍在，**必须显式 `off`**。`off` 会检查命令退出码，并复核本项目的 HTTPS 443 映射已消失；Tailscale 未就绪或无法确认时返回失败，不能把它当成已经关闭。

> Tailscale 官方说明 `--bg` 映射在设备或 Tailscale 重启后仍可能自动恢复；不要以关终端或重启电脑代替 `off`。后端是否自动启动另行检查，使用前仍须运行 `status`。

## 如果 Tailscale 卡住了（本机现状，2026-10-08）

本机 `tailscaled` 长时间停在 `NoState`，`status` 只报一句 `Tailscale is starting. Please wait.`：

```
unexpected state: NoState
BackendState = NoState · Online = False · DNSName = (空)
```

**第一步：分清是「网络不通」还是「守护进程卡住」**（只读，不改任何设置）：

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" netcheck
```

- 能跑出 DERP 延迟 → **网络没问题，是守护进程卡住**，只能重启服务（本机实测：UDP 正常、香港 DERP 21ms，属这一类）
- 连 DERP 都测不到 → 先解决网络/代理，再谈 Funnel

**第二步：以管理员身份重启服务**。在**管理员** PowerShell 里：

```powershell
Restart-Service -Name Tailscale -Force
```

报「无法打开计算机'.'上的 Tailscale 服务」= 当前不是管理员窗口（权限问题，不是服务本身坏了）。
也可直接在「服务」管理器（`services.msc`）里右键 Tailscale → 重新启动，或从开始菜单退出 Tailscale 客户端再重新打开。

重启后确认 `BackendState` 变成 `Running`，再跑 `scripts\tailscale_funnel.bat status`。

⚠️ **这一步只能由你手工做**：`Restart-Service` 和结束系统服务进程都需要管理员权限，脚本与自动化流程都无权代劳（实测报「拒绝访问」）。任务书里「宿主机重启后需要手动重启也可接受」指的就是这一步。

> ⚠️ `tailscale_funnel.ps1` 内部用 .NET `ProcessStartInfo` 调 `tailscale.exe`。在受限环境（沙箱 / 安全软件拦截）里这个调用会被拦住，**脚本必须在普通终端运行**（双击 `.bat`，或开一个普通 PowerShell 窗口），不要在被拦截的环境里跑。

## 验证清单

开 Funnel 前，本机已按 Funnel 的真实请求形态做过验证（`Host` + `Origin` + `X-Forwarded-Proto: https` 三件套），**16/16 通过**：

| 项 | 结果 |
|---|---|
| 公网来源登录 → 200 且 `role=admin` | ✅ |
| 公网来源下 cookie 带 `Secure` | ✅ |
| 错误口令 → 401 | ✅ |
| 非法 `Origin` → 403（拒绝跨站变更请求） | ✅ |
| 非法 `Host` → 403（仅允许本机访问） | ✅ |
| 未登录读取列表/统计/分析/原件/导出/配置/备份 → 401 | ✅ |
| 带会话 cookie 的公网来源可读列表/统计 | ✅ |
| 本机 http 下 cookie **不带** `Secure` | ✅ |
| 伪造会话 token → 401 | ✅ |

本轮只在这台电脑上验收：若 Tailscale 恢复在线，用本机浏览器打开上述公网 HTTPS 域名并检查登录及主要只读流程，结束后立即 `off`。这**不能证明异地网络可达**；未来正式交付远程使用时，仍需用另一网络复验。

### 2026-10-10 下午实测（单目录迁移后）

- Tailscale：`BackendState=Running`、`Online=True`、`DNSName=wandertheearth.tail3e9b7d.ts.net.`；开 Funnel 前 `funnel status --json` 为 `{}`。
- 开 Funnel 前本机检查：未鉴权 `GET /api/document/list`、`/api/document/analytics` → **401**；错误 `Origin` 与**缺失 `Origin`** 的 `POST /api/auth/login` → **403**；正确公网域名 `Origin` → 放行（返回 401＝口令错，非 403）。`.env` 的 `DOC_OCR_DEMO_PUBLIC_ORIGIN` 与 tailnet 域名**完全一致**。
- 映射核对：`TCP.443.HTTPS=true`、`AllowFunnel[域名:443]=true`、`Handlers./.Proxy=http://127.0.0.1:<端口>`。
- 云端只做**一次**生成测试，且只喂**合成资料**：先建只含合成件的隔离库（已剔除唯一一份真实发票），再 `--bg` 指向该隔离服务的端口。
- ⚠️ **异地验收未完成**：本机解析 `*.ts.net` 会命中 Tailscale 内网地址并**直连本机**（另有 curl 走 schannel 报 `CRYPT_E_REVOCATION_OFFLINE`），所以**本机打开公网域名不算异地验收**。真正的手机移动网络验收须由用户完成；**未获确认前不得声称公网验收通过**。

## 已知坑（都是踩过的）

- **cookie 的 `Secure` 必须跟着实际协议走**。写死 `True` 时，本机 `http://127.0.0.1` 下浏览器根本不回传 cookie，现象是「登录返回 200 但下一个请求仍 401」，看着像权限没生效。判据：`X-Forwarded-Proto` 为 `https` 或 `request.url.scheme == https`。
- **不要用 `--https` 之外的自定义端口**去碰运气，Funnel 只认 443/8443/10000。
- **`.env` 改动必须重启后端**才生效（配置在导入时读取，`--reload` 只监听 `.py`）。
- 不要把口令贴进仓库、截图或 PPT；`.env` 已被 `.gitignore` 排除，提交前用 `git status` 复核。

## 关闭后

1. `scripts\tailscale_funnel.bat off`，确认退出码为零且本项目映射已消失。若 Tailscale 为 `NoState`，脚本无法核实关闭状态，须待服务恢复后重查。
2. 等识别/导出任务结束，停掉项目服务。
3. 如需彻底回到本机模式：`.\.venv\Scripts\python.exe scripts\setup_demo_auth.py --disable`，再 `scripts\start_all.bat`。重启会清空内存中的全部会话。

依据：[Funnel 官方文档](https://tailscale.com/docs/features/tailscale-funnel) · [CLI 命令参考](https://tailscale.com/docs/reference/tailscale-cli/funnel)
