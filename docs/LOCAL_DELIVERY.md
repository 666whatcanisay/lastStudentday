# Windows 本机交付与维护

本版采用单机、单进程 FastAPI + SQLite。不要设置多 worker、不要使用 `--reload`，不要直接向公网暴露端口。公开仓库只提供源码、测试与合成语料，不包含本机数据、模型或密钥；克隆后须按下文完成安装和前端构建。

演讲时若要临时通过 Tailscale Funnel 开放 HTTPS 公网入口，必须先启用演示登录并完成权限验收；具体顺序见 [Tailscale 演示操作卡](TAILSCALE_DEMO.md)。应用本身仍只监听 `127.0.0.1`。

## 1. 使用已有环境

项目根目录包含 `app/`、`frontend/`；`.venv/` 需由使用者自行创建。

1. 双击 `scripts/start_all.bat`。它检查虚拟环境与前端构建、启动本机服务、等待健康检查后打开浏览器。
2. 访问 `http://127.0.0.1:8000`。仅支持 `localhost` 或 `127.0.0.1`，不使用远程 IP。
3. 停止前等待识别、导出、问答等操作结束，再双击 `scripts/stop_all.bat`。Windows 下对身份核验通过的服务 PID 执行进程终止，未完成请求会被中断；下次启动检查 SQLite 与恢复日志。
4. 端口被占用时脚本退出，不结束未知进程。确认旧服务归属后处理，或在 PowerShell 运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_all.ps1 -Port 8001`。

首次启动已有数据会执行数据库迁移，迁移前生成本地备份；请先另存已有 `data/` 和 `uploads/`。克隆的新环境不附带正式数据。

## 2. 从干净环境安装

需要 Python 3.12 或 3.13，以及 Node.js/npm。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe scripts/install_dependencies.py
.\.venv\Scripts\python.exe scripts/download_model.py
cd frontend
npm ci
npm run build
cd ..
.\scripts\start_all.bat
```

依赖安装、模型下载需要联网，安装完成后的本地核心功能不需要云端。安装脚本先安装其余依赖，再用 `--no-deps` 安装 RapidOCR，避免同时引入两个 OpenCV 发行包，并在最后导入全部关键运行时依赖做硬性自检。已用全新 Python 3.12 虚拟环境验证安装成功；当前正式环境 Python 3.13 也通过全部功能回归。Docker 配置属于历史材料，不纳入本版部署验收。

无需 LLM Key 即可使用 OCR、校对、统计、PDF/Excel 导出和本地检索。若自行配置 `.env` 中的模型连接，主动发送问答会将相关片段和对话上下文传到该服务；不要输入不允许外传的内容。不要把 `.env` 发给他人。

## 3. 日常闭环

- 文档管理：上传图片/PDF → 识别 → 查看原件与有效文本 → 修改类型和字段 → 保存，可选标记已校对。
- 统计分析：仅有效非 Mock 且未删除文档参与；日期未知独立呈现；不同类型和币种不会混合求和；点击图表查看同过滤条件的明细。
- 导出：单份 PDF 可追加原件；批量 PDF ZIP 上限 20 份，部分失败看 ZIP 内清单；Excel 按类型分工作表。
- 问答：使用当前有效版本检索；修改/删除后旧对话引用可能过时，应刷新检索上下文并重新提问。
- 数据管理：删除先入回收站；永久删除不可撤销，请先备份。

## 4. 备份与恢复

备份：在数据管理点击“立即备份并下载”。新的写请求在备份窗口内返回忙；已开始的 OCR/索引允许完成，等待超过 60 秒取消本次备份。下载的 ZIP 未加密，包含原件、数据库、回收站、版本和问答记录，不包含 Key、模型或虚拟环境。请另存其他磁盘。

恢复：等待任务完成，双击 `scripts/restore_backup.bat`，选择 ZIP 并确认覆盖。脚本先核对路径、清单/哈希、数据库完整性，再核验并停止本应用服务，取得数据目录锁、生成恢复前快照、切换数据、修正原件路径并升级数据库。超过 20 GiB 解包数据的备份会被拒绝。只支持本应用认可的备份和 schema 版本。

恢复成功后**手动运行 `scripts/start_all.bat`**，等待“服务已就绪”，核对文档数并打开一份原件。恢复脚本不会自动重新开启网页。恢复前快照保存在目标数据根目录的 `data/pre_restore_snapshot_*`，不会自动删除；恢复日志在 `data/restore_log.json`。切换中断后下次启动先回滚到快照；若快照损坏或回滚失败，应用拒绝启动，保留证据，不要自行删日志强行启动。

若代码与数据分开，在本机 `.env` 设置 `DOC_OCR_DATA_DIR` 为同时包含 `data/` 和 `uploads/` 的绝对目录。启动、停止、恢复均读取同一份设置；终端已设置的同名环境变量优先。不要填到数据库文件本身，也不要把本机 `.env` 交给公开仓库。未配置时仍使用项目根目录。

## 5. 开发验证

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify_delivery.py --phase all
```

统一验证默认在系统临时目录建立独立数据副本，清空真实云端配置；按 P1–P4 四个功能包集中验证。退出码非零表示失败。结果见 `reports/acceptance/p1.json` 至 `p4.json`。公开仓库不包含旧库/原件冻结夹具，依赖该夹具的两项检查会明确标记为跳过；不要用真实 `data/` 作为测试目录。

备份和报告测试产物仅含合成语料。本轮验证由实现者执行，不冒充另一位独立验收人。验证不会主动调用真实付费云端；Docker 不属于正式交付入口。

## 6. 本机当前部署状态（2026-10-10）

- **单目录**：唯一活动项目 `C:\Users\27715\Desktop\doc-ocr-system-github`，代码与数据同目录；旧目录已归档到 `E:\DocOCRArchive\doc-ocr-system-legacy-20261010_1249`（哈希校验一致，**不永久删除**）。
- **数据根**：`.env` 的 `DOC_OCR_DATA_DIR` 指向上述新目录（`.env` 被 gitignore，勿外发）。迁移前后已逐项比对一致，恢复前快照在 `data/pre_restore_snapshot_*`。
- **登录后自启**：计划任务 `DocOCR-AutoStart`（当前用户登录时触发 `scripts\start_all.ps1 -NoBrowser`）。**手动触发已实测通过；重启后是否自启未实测**。手动启停仍可用 `scripts\start_all.bat` / `stop_all.bat`。
- **单实例锁**：同一数据目录同时只能有一个后端进程；第二个进程会因 `PermissionError` 启动失败。要并行必须换 `DOC_OCR_DATA_DIR`。
- **公网入口**：按需用 `scripts\tailscale_funnel.bat up|status|off`。开之前必须确认未登录返回 **401**，否则**绝不能开**。当前为**单口令、登录即完整管理员**，公网开放等于把全部正常文档的读写权限交给任何拿到链接与口令的人，且**无法区分操作人**——这是已接受但必须显著标注的例外，**不得当作企业级安全**。
