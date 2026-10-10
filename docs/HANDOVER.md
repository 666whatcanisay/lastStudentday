# 从这里接手：智能文档工作台 v1.1.0

一句话：它把图片/PDF 中的文字变成可校对、可统计、可导出、可追溯的文档资料，再用检索帮助人查找答案。它不是会自动确认所有业务事实的“万能财务系统”。

**交给下一位 AI 开发前**，先看 [当前审计底稿](AI_PROJECT_AUDIT.md)、[一次性执行方案](AI_EXECUTION_PLAN.md)、[验收单](AI_ACCEPTANCE.md) 和[可复制的完整提示词](NEXT_AGENT_PROMPT.md)。这些材料区分了源码已证实的问题、带日期的历史记录与仍需运行验证的事项；本文及旧验收数字不能单独代表当前状态。

收尾验收后如要让桌面只剩一个活动项目文件夹，另按[单目录迁移方案](AI_SINGLE_FOLDER_MIGRATION.md)操作；当前正式数据仍由旧目录承载，不能直接删除旧文件夹。

## 阅读顺序

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

精修版代码目录与旧项目数据目录分开。具体本机路径和备份位置记在被忽略的 `reports/LOCAL_HANDOVER.md`。代码读取本机 `.env` 的 `DOC_OCR_DATA_DIR`；值是含 `data/`、`uploads/` 的根目录，而不是 `.db` 文件。启动、停止、恢复必须使用同一份设置。

原代码目录保留作回退参照；不要同时启动两个代码副本占用同一数据目录。原件中的绝对路径是本机状态，迁移到另一台电脑应走备份恢复脚本，不能只拖一个 `.db` 过去。

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

> 接手这个 v1.1.0 文档 OCR 工作台。先读 docs/HANDOVER.md、FINAL_DELIVERY.md 和 TECHNICAL_WALKTHROUGH.md，再核对当前 git 状态与本机配置。默认本轮只维护，不扩功能、不自动推送 GitHub、不自动开放 Funnel。真实 `.env` 和数据不得公开，也不得发送给模型服务。开发测试使用隔离数据；改一个完整功能后再集中测试。保持现有 API、角色权限、版本冲突、统计口径和离线恢复机制。未知业务日期继续未知，不能凭文件名补值；未明确标币种的发票/合同按项目约定默认 CNY，明确外币不可覆盖。提交前运行 pytest、前端构建和适用的 P1–P4 检查；报告实际执行与未执行事项，不能把模拟云端测试说成真实云端验收。
