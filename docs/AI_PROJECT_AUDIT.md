# 新版项目审计底稿（供下一位 AI 核实）

> 记录日期：2026-10-10。代码主线：`C:\Users\27715\Desktop\doc-ocr-system-github`。旧目录 `C:\Users\27715\Desktop\doc-ocr-system` 仅作对照，禁止修改。本底稿来自源码、文档和 Git 状态的**只读检查**：没有在本轮运行测试、启动服务、检查实时数据库或核对 Tailscale 在线状态。历史报告不是今天的验收结果。

## 判断规则

- **已证实**：当前文件内容或 Git 状态直接证明；不等于功能故障已经在浏览器复现。
- **历史记录**：旧文档记载的某次操作、数据量或验收结果；应保留日期，但不得当作实时状态。
- **待验证**：需要下一位 AI 在隔离环境或本机只读检查后才能下结论。失败项须记录复现步骤和证据，再决定是否改代码。
- 优先级：P0 会误导交接或损坏已有工作，P1 影响可信交付，P2 是低成本完善；**数据中确实没有依据的字段不是程序故障**。

## 已证实的问题与限制

| 编号 | 优先级 | 发现与证据 | 影响 | 下一位 AI 的处理 |
| --- | --- | --- | --- | --- |
| A01 | P0 | `frontend/src/App.vue` 已有第五个“项目介绍”页签，`ProjectIntro.vue`/`ProjectTechDialog.vue` 展示六个技术专题；`docs/HANDOVER.md`、`docs/QUICK_START_CN.md` 和 `docs/PROJECT_GUIDE.html` 仍让接手者“记住四个板块”。 | 页面、培训和演讲材料对不上。 | 核对实际页面后统一为“四个业务板块＋一个项目介绍页签”，不要把介绍页说成新的业务接口。 |
| A02 | P0 | `docs/PRESENTATION_CN.md` 主稿仍写“公开演示使用访客与管理员两种权限”，90 秒简版仍说“不知道的日期、币种不猜填”；当前 `app/services/demo_auth.py` 支持单口令即完整管理员，`app/services/extractor.py` 对未明确标币种的发票/合同默认 CNY。 | 上台会讲错权限和统计含义。 | 改为“当前部署统一口令、登录即管理员；未标币种默认 CNY 是业务假设，明确外币不覆盖；业务日期无依据仍留空”。保留双角色能力只作为代码兼容说明。 |
| A03 | P0 | `docs/FINAL_DELIVERY.md` 开头有 2026-10-08 的 r3/CNY 补充，正文仍以“均在 r2”“10 份币种未知”“42/42、16/16”叙述旧快照。 | 读者可能把历史数据和历史测试当作现状。 | 分开写“当时验收”和“本次实际复核”；凡未重新读取的数据和未重新跑的测试都标历史。不得凭文件名补日期或擅自改正式库。 |
| A04 | P0 | 本次只读 `git status --short` 显示 **14 个已跟踪文件有改动、4 个新文件未跟踪**。详见下方快照。 | 执行 AI 若从旧提交重做、清理工作区或覆盖文件，会丢失已做的币种规则和介绍页。 | 先保存变更清单和差异；逐项理解后在原有内容上增量修改。禁止 `reset --hard`、丢弃工作树或把未跟踪文件视为垃圾。 |
| A05 | P1 | `frontend/package.json` 只有 `dev`、`build`、`preview`，没有前端测试命令；六个弹窗目前为静态内容＋交互组件。 | 构建成功无法证明弹窗、跳转、权限提示和手机布局正确。 | 以一次集中浏览器验收覆盖 1440/768/390px、六专题、关闭/Esc、跳转及受限角色；不要仅凭截图或编译成功宣布通过。 |
| A06 | P1 | `app/services/ocr_engine.py` 在 RapidOCR 和 Tesseract 都不可用时返回 Mock 文本；前端 `DocManager.vue` 有 Mock 告警，统计排除已知 Mock。 | 识别 API 可返回流程结果，但那不是真实 OCR；演讲容易误报“识别成功”。 | 检查 `/api/health` 返回的 `data.ocr_engine.engine_type`，必须记录是 `rapidocr`、`tesseract` 还是 `mock`；Mock 时修依赖/环境，不能用模拟结果验收真实 OCR。 |
| A07 | P1 | 当前单口令模式登录即管理员；`DemoAuth` 会话保存在进程内存，服务重启即失效。 | 持有口令者可做全部管理操作，且无法按人追责；不是长期多用户权限系统。 | 本轮只记录风险、验证未登录拒绝和口令模式，不改账号架构。公网是否可用单独验收。 |

### A04：本轮看到的工作区快照

已跟踪但修改：`README.md`、`app/services/extractor.py`、`app/services/field_registry.py`、`docs/FINAL_DELIVERY.md`、`docs/HANDOVER.md`、`docs/PRESENTATION_CN.md`、`docs/PROJECT_GUIDE.html`、`docs/QUICK_START_CN.md`、`docs/TECHNICAL_WALKTHROUGH.md`、`frontend/src/App.vue`、`frontend/src/components/AnalyticsPanel.vue`、`scripts/finalize_legacy_review.py`、`tests/test_legacy_field_rules.py`、`tests/test_review_finalization.py`。

未跟踪：`frontend/src/components/ProjectIntro.vue`、`frontend/src/components/ProjectTechDialog.vue`、`frontend/src/components/projectIntroContent.js`、`scripts/apply_currency_default.py`。

这只是审计时点的快照。执行前必须重新运行 `git status --short`；本交接包自身也会成为新增文件，不可拿上述数字当作最终工作区状态。

## 历史记录，不是当前实测

| 来源 | 曾记录的结果 | 本轮表达方式 |
| --- | --- | --- |
| `docs/FINAL_DELIVERY.md`（2026-10-08） | 26 份有效旧文档按当时资料分为 5 发票、5 合同、16 其他；另有 1 份识别失败记录；11 份业务日期未知；10 份未标币种发票/合同后来按约定保存为 CNY。 | “2026-10-08 的交付快照”；实际数量、版本与质量指标由执行 AI 只读复核。未知业务日期保持未知，不为追求数字好看而填值。 |
| `docs/FINAL_DELIVERY.md`（2026-10-08） | 后端 42/42、P1–P4 16/16、前端构建与当时浏览器检查通过。 | 只作历史证据；当前修改之后必须重新运行并记录新的实际结果、跳过项和失败项。 |
| `docs/FINAL_DELIVERY.md`、`docs/TAILSCALE_DEMO.md`（2026-10-08） | Tailscale 曾为 `NoState`，公网连通未完成。 | 当前状态未知；先只读查询。不得把请求模拟测试称为公网或异地连通验收，也不得为本轮审计开启 Funnel。 |

## 待执行 AI 查证的问题

1. **当前真实状态**：隔离环境运行 `pytest`、P1–P4 和前端构建；记录准确通过数、跳过原因、测试日期和代码基线。若服务正使用同一份前端构建目录，先隔离构建或安排停机，避免覆盖运行中的静态资源。
2. **功能与页面**：五个页签、六个技术弹窗在 1440/768/390px 的实际行为；介绍页不应请求或修改业务数据，受限角色不得通过介绍页进入数据管理。
3. **真实 OCR 与数据质量**：本机实际引擎类型；当前有效文档、未知业务日期、币种缺失统计。数据核对只读；规则上“未标币种默认 CNY”不能遮盖明确外币或多币种冲突。
4. **公网边界**：只读查看 Tailscale 当前状态与 Funnel 映射，不开启或关闭；本轮若没有异地网络实测，则报告“未验收”。

若查出新问题，先写复现条件、预期/实际和影响，再按 `AI_EXECUTION_PLAN.md` 的范围决定修复；不得凭猜测扩大工程。
