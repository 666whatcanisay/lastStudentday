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

`up` 会先自检「未登录返回 401」，再检查 Tailscale 是否在线，最后才建立映射；任何一步不满足都会停下并给出原因，不会带病开放。

关闭：

```powershell
scripts\tailscale_funnel.bat off
```

`up` 用的是 `--bg`（后台保持），所以关掉终端映射仍在，**必须显式 `off`**。`off` 会打印 `funnel status --json`，结果应为 `{}` 或不再含 `Foreground` / `AllowFunnel`。

> 本机重启后 Tailscale 与后端都不会自动带起，手动重跑上面三步即可。

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

开启后建议再用手机关掉 Wi-Fi、走移动数据打开公网链接复验一次。

## 已知坑（都是踩过的）

- **cookie 的 `Secure` 必须跟着实际协议走**。写死 `True` 时，本机 `http://127.0.0.1` 下浏览器根本不回传 cookie，现象是「登录返回 200 但下一个请求仍 401」，看着像权限没生效。判据：`X-Forwarded-Proto` 为 `https` 或 `request.url.scheme == https`。
- **不要用 `--https` 之外的自定义端口**去碰运气，Funnel 只认 443/8443/10000。
- **`.env` 改动必须重启后端**才生效（配置在导入时读取，`--reload` 只监听 `.py`）。
- 不要把口令贴进仓库、截图或 PPT；`.env` 已被 `.gitignore` 排除，提交前用 `git status` 复核。

## 关闭后

1. `scripts\tailscale_funnel.bat off`，并从公网确认链接已不可达。
2. 等识别/导出任务结束，停掉项目服务。
3. 如需彻底回到本机模式：`.\.venv\Scripts\python.exe scripts\setup_demo_auth.py --disable`，再 `scripts\start_all.bat`。重启会清空内存中的全部会话。

依据：[Funnel 官方文档](https://tailscale.com/docs/features/tailscale-funnel) · [CLI 命令参考](https://tailscale.com/docs/reference/tailscale-cli/funnel)
