# 工作台精修与数据切换说明

本轮只精修现有文档管理、统计、智能问答与数据管理，不新增模型或部署方式。代码主线在独立 GitHub 发布副本中；本轮没有推送远程仓库。

2026-09-27 收尾更新：用户已确认保守数据整理与新版切换。26 份已通过 API 保存并标记校对，独立环境与配置一致性已补齐；以下隔离验收记录是精修阶段过程记录，当前结论以 `FINAL_DELIVERY.md` 为准。

## 代码边界

- 文档页由 `UploadQueue.vue`、`DocumentList.vue`、`DocumentDetail.vue` 组成；上传队列、筛选/选择/批量导出、原件校对/版本/单份报告各自持有状态。
- 跨页使用 `doc-ocr-documents-changed` 领域通知更新概览、统计与问答文档选项。数据维护页恢复或永久删除也发出通知。页签不再直接调用其他页签的 `refresh()`。
- 后端统计与导出分别在 `document_analytics.py`、`document_export.py`；注册顺序保证固定路径先于 `/{doc_id}`。原 URL、响应结构、演示权限、数据库结构不变。
- 手机文档列表使用卡片；筛选、批量操作与问答检索过程可按需展开。管理员复杂维护仍建议桌面使用。

## 私有审核与正式数据

隔离审核可运行：

```powershell
python -m scripts.audit_legacy_documents --db data/audit_copy/doc_ocr_v2.db --output reports/legacy_review.csv
```

`data/audit_copy/` 和 `reports/` 在仓库忽略规则内。审核 CSV 含文件名与业务字段，不得公开或推送。审核汇总见本机 `reports/legacy_review_summary.md`；它不是自动修复指令。只有用户逐项确认后，才能先备份正式数据，再通过现有文档 PATCH 接口按 `expected_revision` 保存并保留版本历史；未知字段继续留空。严禁直接 UPDATE 正式库。

## 已完成的隔离验收

- 后端 `pytest -q`：35 个测试通过；`scripts/verify_delivery.py --phase all`：P1–P4 共 16/16 通过、无跳过。
- 前端 `npm run build` 通过。浏览器在 1440、768、390px 下检查文档、统计和问答，页面无横向溢出；手机访客可打开原件校对、上传并识别样本、展开问答检索过程。
- 隔离演示服务验证了访客 PDF 报告与选中文档 Excel 下载。上传验收时修正了表单封装：文件放入 `FormData.file`，由浏览器生成 multipart boundary。
- 精修阶段隔离服务与浏览器会话已关闭。这组隔离结果本身不等于正式切换；正式切换与冒烟已在随后收尾阶段执行，见 `FINAL_DELIVERY.md`。

## 验收与切换门槛

1. 在隔离数据上通过 `pytest`、`npm run build` 和 `scripts/verify_delivery.py --phase all`，再用浏览器检查 1440/768/390px 的文档、统计和问答主流程。
2. 经用户批准数据审核结论后，确认 Funnel 已关闭，再停止旧服务并另存可离线恢复的正式备份。
3. 新代码连接原数据目录，先在本机做未登录/访客/管理员权限、上传识别、校对冲突、统计与 PDF/Excel 冒烟检查；失败时停止新服务、恢复旧代码及备份。完成前不要重新开放 Funnel。
4. 用户确认验收后再决定是否推送 GitHub。本轮不自动发布或替换正在运行的服务。
