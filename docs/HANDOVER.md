# 从这里接手：智能文档工作台 v1.1.0

一句话：它把图片/PDF 中的文字变成可校对、可统计、可导出、可追溯的文档资料，再用检索帮助人查找答案。它不是会自动确认所有业务事实的“万能财务系统”。

**当前长任务入口**：先读[单目录、验收与远程交付执行书](AI_ONE_SHOT_CLOSEOUT.md)，再把[一次性提示词](NEXT_AGENT_ONE_SHOT_PROMPT.md)交给同一个执行 AI。以下原有审计与验收材料仍供核实，但旧的阶段性“禁止迁移/开 Funnel/云端调用”限制已被本次授权取代；本文和旧验收数字不能单独代表当前状态。

本次长任务包含[单目录迁移方案](AI_SINGLE_FOLDER_MIGRATION.md)原来计划的工作，但以新版执行书的门槛为准。执行前须重新核对正式数据根；旧文件夹只能在完整备份和迁移验收后移往 E 盘归档，不能直接删除。

## 阅读顺序

如果目的是准备简历和面试，先看 [通俗技术学习指南](PROJECT_LEARNING_GUIDE_CN.md)；手机阅读可打开 [独立 HTML 版](PROJECT_LEARNING_GUIDE_CN.html)，再按下面顺序查细节。

1. 用浏览器打开同目录 `PROJECT_GUIDE.html`：先看整个系统，不需要安装任何文档阅读器。
2. [30 分钟快速上手](QUICK_START_CN.md)：按真实页面走一遍。
3. [技术与工作流](TECHNICAL_WALKTHROUGH.md)：把每一步对应到代码。
4. [上台讲解与答辩](PRESENTATION_CN.md)：完整讲稿、演示顺序、常见追问。
5. [交付状态](FINAL_DELIVERY.md)、[本机安装维护](LOCAL_DELIVERY.md)、[公网演示操作卡](TAILSCALE_DEMO.md)：确认运行边界与恢复办法。

建议先操作再读源码。第一次先记住四个业务板块：文档管理、统计、智能问答、数据管理；另有第五个“项目介绍”讲解页签。

## 接收的东西分两包

- 源码包：后端、前端、迁移、测试、脚本、合成样本与以上材料。源代码可以公开；本轮只在本机封版，不自动推送 GitHub。
- 私有运行包：本机 `.env`、业务数据库、原件、备份和口令。必须另行安全移交，不能放进公开源码包。备份 ZIP 未加密，里面有全部业务内容。

源码包没有 `.venv`、`node_modules`、模型和前端构建产物，是正常的；按安装说明建立这些文件。不要把虚拟环境复制到另一台电脑当作正式安装。

## 本机的代码与数据分工

**2026-10-10 下午起已归并为单目录**：唯一活动项目是 `C:\Users\27715\Desktop\doc-ocr-system-github`，代码与数据（`data/`、`uploads/`）都在这一目录内。旧目录已整体归档到 `E:\DocOCRArchive\doc-ocr-system-legacy-20261010_1249`（逐文件哈希校验一致，**不永久删除**），桌面不再保留副本。

代码读取本机 `.env` 的 `DOC_OCR_DATA_DIR`；值是含 `data/`、`uploads/` 的根目录，而不是 `.db` 文件。启动、停止、恢复必须使用同一份设置；**同一数据目录只能由一个服务占用**（单实例锁）。本机路径、备份位置、哈希与回滚点记在被忽略的私有交接记录里（清单见 [交付状态](FINAL_DELIVERY.md) 第三段）。原件中的绝对路径是本机状态，迁移到另一台电脑应走备份恢复脚本，不能只拖一个 `.db` 过去。

按用户后续要求，登录自启任务 `DocOCR-AutoStart` 已停用。现在从项目根目录双击 `启动项目.bat` 手动启动；该按钮使用相对路径调用现有启动脚本，搬动整个项目文件夹后无需改写启动路径。

## 最少需要认识的代码

| 入口 | 做什么 | 先看什么 |
| --- | --- | --- |
| `app/main.py` | 应用装配、生命周期、路由、静态前端 | 服务启动怎样取得数据目录锁 |
| `app/config.py`、`app/local_settings.py` | 路径与本机配置 | 环境变量优先，测试可跳过真实 `.env` |
| `app/routers/file.py`、`ocr.py` | 上传与识别 | 文件校验、SHA-256 去重、逐页处理 |
| `app/services/preprocess.py`、`ocr_engine.py` | 图像准备和文字识别 | 预训练模型与本地推理，不是自研训练 |
| `app/services/field_registry.py` | 类型、字段、规范化规则 | 日期为何会留空、币种明确标注如何识别；未标币种在抽取流程默认 CNY |
| `app/routers/document.py`、`app/services/revisions.py` | 编辑、校对、历史 | `expected_revision` 和有效版本 |
| `app/routers/document_analytics.py`、`document_export.py` | 统计、导出 | 同一有效快照，同币种才汇总 |
| `app/services/rag_indexer.py`、`rag_pipeline.py` | 建索引、检索、问答 | 当前版本才能进入检索 |
| `frontend/src/components/DocManager.vue` | 文档页组织 | UploadQueue / DocumentList / DocumentDetail |
| `frontend/src/documentEvents.js` | 同页面板块更新通知 | 文档变化后独立刷新；不是跨用户实时推送 |
| `scripts/start_all.ps1`、`service_control.py`、`restore_backup.py` | 启停与离线恢复 | 只停止身份核验过的本应用进程 |

## 交接后的工作规则

先备份再改正式数据；功能改完集中验证，不必每行代码都测一遍。不要改变现有 URL、权限与字段含义来“修好”演示数据；不要添加多 worker 或 `--reload`；不要在未启用访问口令时开放 Funnel。新功能并不是本次交付目标。

## 给接手开发者的提示词

> 以下是早期“仅维护、不迁移”的通用提示，不适用于本次已获授权的单目录与公网长任务；本次请使用 [NEXT_AGENT_ONE_SHOT_PROMPT.md](NEXT_AGENT_ONE_SHOT_PROMPT.md)。

> 接手这个 v1.1.0 文档 OCR 工作台。先读 docs/HANDOVER.md、FINAL_DELIVERY.md 和 TECHNICAL_WALKTHROUGH.md，再核对当前 git 状态与本机配置。默认本轮只维护，不扩功能、不自动推送 GitHub、不自动开放 Funnel。真实 `.env` 和数据不得公开，也不得发送给模型服务。开发测试使用隔离数据；改一个完整功能后再集中测试。保持现有 API、角色权限、版本冲突、统计口径和离线恢复机制。未知业务日期继续未知，不能凭文件名补值；未明确标币种的发票/合同按项目约定默认 CNY，明确外币不可覆盖。提交前运行 pytest、前端构建和适用的 P1–P4 检查；报告实际执行与未执行事项，不能把模拟云端测试说成真实云端验收。
