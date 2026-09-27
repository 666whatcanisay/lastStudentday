# 轻量化智能文档 OCR 与信息抽取系统 · v1.1.0

接手或准备演讲，先看 [接手入口](docs/HANDOVER.md)。离线打开 `docs/PROJECT_GUIDE.html` 可按“功能 → 工作流 → 30分钟上手 → 上台讲解”阅读；正式交付状态见 [验收说明](docs/FINAL_DELIVERY.md)。

这是一个在 Windows 本机运行的文档处理项目：上传图片或 PDF，经预处理与 OCR 识别后抽取发票、合同等结构化字段，并支持人工校对、版本记录、统计图表、PDF/Excel 导出和带原文引用的检索问答。后端使用 FastAPI、SQLite、RapidOCR 与本地向量模型；前端使用 Vue 3、Element Plus 和 ECharts。核心流程可在 CPU 上运行，不要求 GPU。

本仓库**只包含源码、测试及合成测试语料**，不包含用户文档、数据库、备份、模型权重、口令或 API Key。默认仅监听 `127.0.0.1`，不是直接面向公网的服务。

## 功能

- 文档管理：图片/PDF 上传、逐页识别、字段提取、原件对照校对、版本与回收站。
- 数据分析：按文档类型、日期及金额等维度展示图表，并可查看对应明细。
- 导出与保护：单份中文 PDF、批量 PDF ZIP、分类 Excel、全量备份和离线恢复。
- 智能问答：向量检索与 BM25 融合，展示引用及对应原文；未配置云端 Key 时降级为本地检索结果。
- 演示模式：可选的访客/管理员口令及角色权限，配合 Tailscale Funnel 临时开放演示入口。公网开放前须按[演示操作卡](docs/TAILSCALE_DEMO.md)完成防护验收。

## Windows 本机安装

需要 Python 3.12 或 3.13、Node.js 18+ 与 npm。以下命令在仓库根目录的 PowerShell 中运行；依赖和模型下载需要网络。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe scripts\install_dependencies.py
.\.venv\Scripts\python.exe scripts\download_model.py
Push-Location frontend
npm ci
npm run build
Pop-Location
.\scripts\start_all.bat
```

服务就绪后打开 <http://127.0.0.1:8000>。停止时先等待识别、导出等任务结束，再运行 `scripts\stop_all.bat`。请保持单进程启动，不要添加多 worker 或 `--reload`；首次打开已有数据时先另存 `data/` 与 `uploads/`。更完整的安装、备份与恢复说明见[本机交付说明](docs/LOCAL_DELIVERY.md)。本仓库没有经过容器部署验收，不提供 Docker 作为正式启动方式。

不设置 `.env` 也可使用 OCR、校对、统计、导出和本地检索。需要云端生成式问答时，复制 `.env.example` 为 `.env`，仅在本机填写 `LLM_API_KEY` 等配置。主动发起云端问答会把相关文档片段发送给所配置的模型服务；请先确认数据允许外传。不要提交 `.env`、口令或真实业务数据。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -X utf8 scripts\verify_delivery.py --phase all
```

交付验证会使用独立临时数据并禁用真实云端 Key，结果写在被忽略的 `reports/acceptance/`。公开仓库不附带旧库及原件的冻结夹具；缺少它时，旧库迁移和冻结检索对照两项会在报告中明确标为“跳过”，其余用例照常执行。前端改动后在 `frontend/` 执行 `npm run build`；生成的 `app/static_vite/` 不纳入版本库。

## 主要目录

| 目录 | 内容 |
| --- | --- |
| `app/` | FastAPI 接口、OCR、抽取、检索、导出与数据保护逻辑 |
| `frontend/` | Vue 3 前端源码 |
| `alembic/` | SQLite 数据库迁移 |
| `scripts/` | 安装、启动、维护和交付验证工具 |
| `tests/`、`test_corpus/` | 自动化测试与合成 PDF 语料 |

运行时生成的 `data/`、`uploads/`、`models/`、`reports/` 和 `output/` 均留在本机，不应加入公开仓库。
