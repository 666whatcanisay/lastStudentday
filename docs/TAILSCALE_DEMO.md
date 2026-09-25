# Tailscale Funnel 演讲演示操作卡

此入口面向公网。拿到链接和访客口令的人可查看、修改、导出当前项目中的所有正常文档。访客之间共用同一套数据；统一口令无法区分具体操作人。演示期间仅管理员可删除、管理回收站、下载全量备份或手动维护索引。离线恢复脚本不通过网页开放。

## 演讲前准备

1. 等待当前识别和导出结束，在「数据管理」执行一次全量备份，并将 ZIP 另存到其他磁盘。检查当前原件及文字是否适合让访客查看、导出。
2. 从 <https://tailscale.com/download> 安装 Windows 客户端，登录你的 tailnet。到 Tailscale 管理台 DNS 页面确认 MagicDNS 和 HTTPS 已开启，记录本机准确的 `设备名.tailnet.ts.net` 域名。不要先启用 Funnel。
3. 在项目根目录执行：

   ```powershell
   .\.venv\Scripts\python.exe scripts\setup_demo_auth.py --origin "https://设备名.tailnet.ts.net"
   ```

   脚本只在被 `.gitignore` 忽略的 `.env` 中保存两份加盐口令哈希，不保存明文；会在终端各显示一次访客口令和管理员口令。请分别记录，勿公开管理员口令。重复运行会轮换两份口令。
4. 双击 `scripts/stop_all.bat` 停止旧服务，随后运行 `scripts/start_all.bat` 启动演示模式。当前监听地址仍为 `127.0.0.1:8000`。在另一个终端检查：

   ```powershell
   curl.exe -i http://127.0.0.1:8000/api/document/list
   ```

   应返回 HTTP 401。若服务未启动或返回 200，不要开启 Funnel。检查 `data/runtime/server.stderr.log` 和 `.env` 中的演示域名配置。服务启动时会检查口令哈希和域名；配置不全会拒绝启动。
5. 确认自动验收通过后，在单独终端执行：

   ```powershell
   tailscale funnel --https=443 8000
   ```

   第一次运行可能要求在浏览器批准 HTTPS/Funnel。命令所在终端保持运行，**不加 `--bg`**。确认命令显示的公网 URL 与配置完全一致。若不一致，先关闭 Funnel，再重新配置域名、重启应用。
6. 用手机关闭 Wi-Fi，走移动数据打开公网 URL，分别验证访客与管理员登录。试用非敏感样例：上传 → OCR → 校对 → 统计 → 问答 → PDF 导出；访客界面不应显示数据管理和删除按钮。确认管理员可打开数据管理。

Funnel 仅支持 HTTPS 公网端口 443、8443、10000，流量有不可调整的带宽限制；DNS 刚启用时最长可能需约 10 分钟生效。演讲前至少完成一次跨网络彩排。依据：[Funnel 官方文档](https://tailscale.com/docs/features/tailscale-funnel)、[CLI 命令参考](https://tailscale.com/docs/reference/tailscale-cli/funnel)。

## 演讲结束

1. 在 Funnel 终端按 `Ctrl+C`，再执行 `tailscale funnel status --json`。前台模式下，普通文本状态可能显示 `No serve config`，即使 `Foreground` 映射仍在运行；以 JSON 中是否仍有 `Foreground` / `AllowFunnel` 为准。若仍显示 443 公网转发，执行 `tailscale funnel --https=443 off`，直至结果为 `{}`，并从公网验证链接已无法访问。
2. 等待在途识别/导出完成，停止项目服务。
3. 在项目根目录执行 `.\.venv\Scripts\python.exe scripts\setup_demo_auth.py --disable`，然后用 `scripts/start_all.bat` 恢复普通本机模式。重启会清除内存中的所有演示会话。
4. 从手机移动数据确认原公网 URL 已无法访问项目。

项目不在公网持续运行；关闭 Funnel 后不应留下后台公开映射。不要把访客或管理员口令贴进仓库、截图或演讲 PPT。
