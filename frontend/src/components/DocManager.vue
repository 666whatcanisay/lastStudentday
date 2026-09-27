<template>
  <!--
    文档管理页签（方案 B）

    相比旧版的核心变化：
      1. 详情改为**左原件 / 右字段+全文**对照布局：图片可缩放、PDF 用浏览器
         原生预览（自带翻页），浏览器无法内嵌时可新窗口打开或下载
      2. 编辑走 PATCH 乐观并发：expected_revision 冲突提示重新载入，不覆盖他人改动
      3. 「保存」只保存有效内容，不自动标校对；另有「保存并标为已校对」
      4. 切换类型需一次确认，确认后列出目标类型字段供**保存前**编辑，不悄悄提交
      5. 上传统一走 axios（旧版是裸 XHR，绕过拦截器拿不到中文错误），
         逐份排队上传+识别，真实显示等待/上传/识别/成功/失败，不显示虚构百分比
      6. 历史只读：每次 OCR/编辑/改类型的时间、原因与完整快照
  -->
  <div class="doc-manager">
    <!-- ===== 统计卡片 ===== -->
    <el-row :gutter="16" class="stat-row">
      <el-col :xs="8" :sm="8" :md="8">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-icon stat-icon-blue"><el-icon><Files /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">文档总数</div>
              <div class="stat-value">{{ stats.total }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="8" :sm="8" :md="8">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-icon stat-icon-green"><el-icon><CircleCheck /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">识别成功</div>
              <div class="stat-value">{{ stats.success }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="8" :sm="8" :md="8">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-icon stat-icon-orange"><el-icon><Clock /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">待校对</div>
              <div class="stat-value">{{ stats.unreviewed }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ===== 引擎降级提示 ===== -->
    <el-alert
      v-if="ocrEngine.engine_type === 'mock'"
      class="mock-alert"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    >
      <template #title>当前为模拟引擎 (Mock)，识别结果是占位文本</template>
      <div>
        RapidOCR 未能加载，系统已降级为模拟引擎。正式识别会被明确拒绝，
        不会生成"识别成功"的占位文档。请检查后端依赖（rapidocr-onnxruntime 等）。
      </div>
    </el-alert>

    <UploadQueue />

    <DocumentList :is-admin="isAdmin" :pending-count="stats.unreviewed" @open-detail="openDetail" />

    <DocumentDetail ref="detailRef" :is-admin="isAdmin" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import UploadQueue from './UploadQueue.vue'
import DocumentList from './DocumentList.vue'
import DocumentDetail from './DocumentDetail.vue'
import { getHealth, getDocumentStats } from '../api/index.js'
import { DOCUMENTS_CHANGED } from '../documentEvents.js'

defineProps({ isAdmin: { type: Boolean, default: true } })
const detailRef = ref(null)
const stats = reactive({ total: 0, success: 0, unreviewed: 0 })
const ocrEngine = reactive({ engine_type: null })

async function loadStats() {
  try {
    const result = await getDocumentStats()
    stats.total = result.data.total
    stats.success = result.data.success
    stats.unreviewed = result.data.unreviewed ?? 0
  } catch (error) { /* API interceptor has shown the error */ }
}
async function fetchHealth() {
  try {
    const result = await getHealth()
    const engine = result.data.ocr_engine || {}
    ocrEngine.engine_type = engine.engine_type || (engine.optimized ? 'rapidocr' : null)
  } catch (error) { /* health badge stays unknown */ }
}
function openDetail(row, edit = false) { detailRef.value?.open(row, edit) }

onMounted(() => {
  fetchHealth()
  loadStats()
  window.addEventListener(DOCUMENTS_CHANGED, loadStats)
})
onUnmounted(() => window.removeEventListener(DOCUMENTS_CHANGED, loadStats))
</script>

<style scoped>
.stat-row { margin-bottom: 10px; }
.stat-row :deep(.el-col) { margin-bottom: 8px; }
.stat-card { border-radius: 10px; border-color: #e4ece9; }
.stat-card :deep(.el-card__body) { padding: 12px 14px; }
.stat-content { display: flex; align-items: center; gap: 12px; min-width: 0; }
.stat-icon {
  width: 38px; height: 38px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 19px; color: #fff;
}
.stat-icon-blue { background: #477f88; }
.stat-icon-green { background: #5b8b76; }
.stat-icon-orange { background: #ac865c; }
.stat-info { flex: 1; min-width: 0; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 21px; font-weight: 600; color: #19343d; white-space: nowrap; }
@media (max-width: 767px) {
  .stat-row { margin-bottom: 5px; }
  .stat-card :deep(.el-card__body) { padding: 9px; }
  .stat-content { gap: 7px; }
  .stat-icon { width: 30px; height: 30px; font-size: 16px; }
  .stat-label { font-size: 11px; }
  .stat-value { font-size: 17px; }
}
</style>
