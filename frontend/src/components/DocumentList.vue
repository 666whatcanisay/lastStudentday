<template>
    <!-- ===== 文档列表区域 ===== -->
    <el-card class="list-card" shadow="never">
      <template #header>
        <div class="card-header">
          <el-icon><Document /></el-icon>
          <span>文档列表</span>
          <span class="pending-count">{{ pendingCount }} 份待校对</span>
          <el-button class="mobile-filter-toggle" size="small" @click="mobileFiltersOpen = !mobileFiltersOpen">
            {{ mobileFiltersOpen ? '收起筛选' : '筛选' }}
          </el-button>
          <div class="header-actions" :class="{ 'is-open': mobileFiltersOpen }">
            <el-input
              v-model="searchKeyword"
              placeholder="搜索文件名或识别内容"
              clearable
              class="search-input"
              @keyup.enter="loadDocuments"
              @clear="loadDocuments"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-button type="primary" @click="loadDocuments">
              <el-icon><Search /></el-icon>搜索
            </el-button>
            <el-radio-group v-model="reviewFilter" size="small" @change="applyReviewFilter">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="pending">待校对</el-radio-button>
            </el-radio-group>
            <el-button @click="loadDocuments">
              <el-icon><Refresh /></el-icon>刷新
            </el-button>
          </div>
        </div>
      </template>

      <div class="selection-bar">
        <span v-if="selectedIds.length">已选 {{ selectedIds.length }} 份</span>
        <span v-else class="selection-hint">勾选下方文档后可批量导出</span>
        <el-checkbox v-model="attachOriginal" size="small" :disabled="!selectedIds.length" title="原件缺失时该份导出失败">PDF 附原件</el-checkbox>
        <el-button size="small" type="primary" :disabled="!selectedIds.length" :loading="exporting" @click="exportReportsSelected">导出 PDF</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="exporting" @click="exportSelected">导出 Excel</el-button>
      </div>

      <el-table
        class="desktop-table"
        :data="documents"
        v-loading="loading"
        @selection-change="handleSelectionChange"
        stripe
        style="width: 100%"
      >
        <el-table-column type="selection" width="45" />
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="file_name" label="文件名" min-width="160" show-overflow-tooltip />
        <el-table-column prop="file_type" label="类型" width="66" align="center">
          <template #default="{ row }">
            <el-tag :type="row.file_type === 'pdf' ? 'danger' : 'primary'" size="small">
              {{ row.file_type === 'pdf' ? 'PDF' : '图片' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="doc_type" label="单据类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="docTypeTag(row.doc_type)">
              {{ docTypeLabel(row.doc_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="88" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本/校对" width="110" align="center">
          <template #default="{ row }">
            <span class="rev-text">r{{ row.current_revision ?? '-' }}</span>
            <el-tag size="small" :type="row.reviewed ? 'success' : 'info'" class="review-tag">
              {{ row.reviewed ? '已校对' : '未校对' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="infer_cost" label="推理耗时" width="98" align="center">
          <template #default="{ row }">
            <span v-if="row.infer_cost">{{ Number(row.infer_cost).toFixed(1) }} ms</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="抽取字段" width="240">
          <template #default="{ row }">
            <div v-if="row.extracts?.some(item => item.field_key && item.field_value)" class="extract-preview">
              <el-tag
                v-for="ext in row.extracts.filter(item => item.field_key && item.field_value).slice(0, 2)"
                :key="ext.id"
                size="small"
                :type="ext.field_value ? 'success' : 'info'"
                class="extract-tag"
              >
                {{ ext.field_name }}: {{ ext.field_value || '未识别' }}
              </el-tag>
            </div>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="upload_time" label="上传时间" width="150" align="center">
          <template #default="{ row }">{{ formatTime(row.upload_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="205" fixed="right" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="viewDetail(row, false)">
              <el-icon><View /></el-icon> 详情
            </el-button>
            <el-button
              v-if="row.current_revision"
              size="small"
              type="warning"
              link
              @click="viewDetail(row, true)"
            >
              <el-icon><Edit /></el-icon> 编辑
            </el-button>
            <el-button
              v-if="row.current_revision"
              size="small"
              type="info"
              link
              @click="reRecognize(row)"
            >
              <el-icon><RefreshRight /></el-icon> 重识别
            </el-button>
            <el-popconfirm v-if="isAdmin" title="确定移入回收站？可在数据管理中恢复" @confirm="handleDelete(row)">
              <template #reference>
                <el-button size="small" type="danger" link>
                  <el-icon><Delete /></el-icon> 删除
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <div class="mobile-doc-list" v-loading="loading">
        <div v-for="row in documents" :key="row.id" class="mobile-doc-card">
          <div class="mobile-doc-title">
            <el-checkbox :model-value="selectedIds.includes(row.id)" @change="checked => toggleMobileSelection(row.id, checked)" />
            <strong :title="row.file_name">{{ row.file_name }}</strong>
          </div>
          <div class="mobile-doc-meta">
            <el-tag size="small" :type="docTypeTag(row.doc_type)">{{ docTypeLabel(row.doc_type) }}</el-tag>
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
            <el-tag size="small" :type="row.reviewed ? 'success' : 'warning'">{{ row.reviewed ? '已校对' : '待校对' }}</el-tag>
            <span>r{{ row.current_revision ?? '-' }}</span>
          </div>
          <!-- 窄屏把「ID / 类型 / 推理耗时 / 上传时间」收成一行次要信息，
               保证手机上也不丢管理员需要的核对字段 -->
          <div class="mobile-doc-sub">
            <span>#{{ row.id }}</span>
            <span>{{ row.file_type === 'pdf' ? 'PDF' : '图片' }}</span>
            <span v-if="row.infer_cost">{{ Number(row.infer_cost).toFixed(1) }} ms</span>
            <span>{{ formatTime(row.upload_time) }}</span>
          </div>
          <div v-if="row.extracts?.length" class="mobile-doc-fields">
            {{ row.extracts.filter(item => item.field_key && item.field_value).slice(0, 2).map(item => `${item.field_name}：${item.field_value}`).join(' · ') || '暂无适用字段' }}
          </div>
          <div class="mobile-doc-actions">
            <el-button type="primary" size="small" @click="viewDetail(row, false)">查看</el-button>
            <el-button v-if="row.current_revision" size="small" @click="viewDetail(row, true)">校对</el-button>
            <el-button v-if="row.current_revision" size="small" @click="reRecognize(row)">重识别</el-button>
          </div>
        </div>
        <el-empty v-if="!loading && !documents.length" description="当前没有符合条件的文档" :image-size="60" />
      </div>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          :layout="paginationLayout"
          background
          @size-change="loadDocuments"
          @current-change="loadDocuments"
        />
      </div>
    </el-card>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDocumentList, reRecognizeOCR, deleteDocument,
  exportExcel, exportReports, saveBlob } from '../api/index.js'
import { DOCUMENTS_CHANGED, notifyDocumentsChanged } from '../documentEvents.js'

const props = defineProps({
  isAdmin: { type: Boolean, default: false },
  pendingCount: { type: Number, default: 0 },
})
const emit = defineEmits(['open-detail'])
const loading = ref(false)
const documents = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchKeyword = ref('')
const reviewFilter = ref('all')
const mobileFiltersOpen = ref(false)
const viewportWidth = ref(window.innerWidth)
const paginationLayout = computed(() => viewportWidth.value < 600
  ? 'total, prev, next' : 'total, sizes, prev, pager, next, jumper')
const selectedIds = ref([])
const attachOriginal = ref(false)
const exporting = ref(false)

const statusText = status => ({ 0: '待识别', 1: '识别成功', 2: '识别失败' }[status] || '未知')
const statusTagType = status => ({ 0: 'info', 1: 'success', 2: 'danger' }[status] || 'info')
const docTypeLabel = type => ({ invoice: '发票', contract: '合同', other: '其他' }[type] || '其他')
const docTypeTag = type => ({ invoice: 'danger', contract: 'warning', other: 'info' }[type] || 'info')
const errMsg = error => error?.msg || error?.response?.data?.msg || error?.message || '未知原因'

// 上传时间按本机时区渲染成「YYYY-MM-DD HH:mm:ss」。
// 后端返回的是不带时区的 ISO 串，直接字符串截断会当成 UTC 显示，
// 必须交给 Date 解析再取本地字段。
function formatTime(time) {
  if (!time) return '-'
  const d = new Date(time)
  if (isNaN(d.getTime())) return String(time)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function viewDetail(row, edit = false) { emit('open-detail', row, edit) }
function updateViewport() { viewportWidth.value = window.innerWidth }
function handleSelectionChange(rows) { selectedIds.value = rows.map(row => row.id) }
function toggleMobileSelection(id, checked) {
  selectedIds.value = checked
    ? [...new Set([...selectedIds.value, id])]
    : selectedIds.value.filter(item => item !== id)
}
function applyReviewFilter() { currentPage.value = 1; loadDocuments() }

async function loadDocuments() {
  loading.value = true
  try {
    const result = await getDocumentList({
      page: currentPage.value,
      page_size: pageSize.value,
      keyword: searchKeyword.value || undefined,
      reviewed: reviewFilter.value === 'pending' ? false : undefined,
    })
    documents.value = result.data.items
    total.value = result.data.total
  } catch (error) { /* API interceptor has shown the error */ }
  finally { loading.value = false }
}

async function reRecognize(row) {
  try {
    await ElMessageBox.confirm(
      '重新识别会基于原件生成新版本，覆盖当前有效结果（历史版本仍可查看）。确认继续？',
      '确认重新识别', { type: 'warning', confirmButtonText: '确认重新识别', cancelButtonText: '取消' })
  } catch { return }
  ElMessage.info(`正在重新识别「${row.file_name}」，请耐心等待`)
  try {
    await reRecognizeOCR(row.id)
    ElMessage.success('重新识别完成，已发布新版本')
    notifyDocumentsChanged()
  } catch (error) {
    ElMessage.error(`重新识别失败：${errMsg(error)}`)
    loadDocuments()
  }
}

async function handleDelete(row) {
  if (!props.isAdmin) return
  try {
    await deleteDocument(row.id)
    ElMessage.success('已移入回收站（可在数据管理中恢复）')
    notifyDocumentsChanged()
  } catch (error) { ElMessage.error(`删除失败：${errMsg(error)}`) }
}

async function runExport(action, label) {
  if (exporting.value) { ElMessage.warning('导出正在进行中，请等待完成'); return }
  exporting.value = true
  try {
    const result = await action()
    if (!result.data || result.data.size === 0) {
      ElMessage.error(`${label}失败：服务端返回了空文件`)
      return
    }
    saveBlob(result.data, result.filename)
    ElMessage.success(`${label}完成：${result.filename || ''}`)
  } catch (error) {
    if (error?.code === 'ECONNABORTED' || /timeout/i.test(error?.message || ''))
      ElMessage.error(`${label}超时：请减少份数后重试`)
  } finally { exporting.value = false }
}

async function exportSelected() {
  const ids = selectedIds.value.slice()
  if (!ids.length) return
  await runExport(() => exportExcel(ids), `导出 Excel（${ids.length} 份）`)
}
async function exportReportsSelected() {
  const ids = selectedIds.value.slice()
  if (!ids.length) return
  if (ids.length > 20) { ElMessage.warning('单次最多导出 20 份，请分批选择'); return }
  if (attachOriginal.value) {
    try {
      await ElMessageBox.confirm(
        '原件缺失会导致该份报告导出失败，原因会记录在 ZIP 清单中。继续？',
        '附原件导出', { type: 'warning' })
    } catch { return }
  }
  await runExport(() => exportReports(ids, attachOriginal.value),
    `导出 PDF 报告（${ids.length} 份）`)
}

onMounted(() => {
  loadDocuments()
  window.addEventListener(DOCUMENTS_CHANGED, loadDocuments)
  window.addEventListener('resize', updateViewport)
})
onUnmounted(() => {
  window.removeEventListener(DOCUMENTS_CHANGED, loadDocuments)
  window.removeEventListener('resize', updateViewport)
})
</script>

<style scoped>
.card-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 600; font-size: 15px; }
.pending-count { font-size: 12px; font-weight: 400; color: #8a6636; background: #f9f3e9; padding: 3px 8px; border-radius: 20px; }
.header-actions { display: flex; align-items: center; gap: 7px; margin-left: auto; flex-wrap: wrap; }
.search-input { width: 220px; }
.mobile-filter-toggle { display: none; }
.selection-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px; margin-bottom: 8px; border: 1px solid #cfdfdc; border-radius: 8px; background: #f0f7f6; font-size: 13px; }
/* 导出入口常驻：旧版一直是「导出PDF报告 (0) / 导出Excel (0)」常显，
   收进勾选后才出现会让管理员找不到批量导出。这里保持可见、未选时禁用。 */
.selection-hint { color: #8a9a97; }
.list-card { border-radius: 10px; border-color: #e4ece9; }
.list-card :deep(.el-card__header) { padding: 11px 15px; }
.list-card :deep(.el-card__body) { padding: 12px 15px; }
.pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 16px; }
.mobile-doc-list { display: none; }
.extract-preview { display: flex; flex-wrap: wrap; gap: 4px; }
.extract-tag { font-size: 12px; }
.rev-text { font-size: 12px; color: #606266; margin-right: 6px; }
.review-tag { transform: scale(0.9); }
.text-muted { color: #909399; }
@media (max-width: 1000px) {
  .card-header { align-items: flex-start; }
  .header-actions { width: 100%; margin-left: 0; }
}
@media (max-width: 900px) {
  .mobile-filter-toggle { display: inline-flex; margin-left: auto; }
  .header-actions { display: none; flex-direction: column; align-items: stretch; width: 100%; }
  .header-actions.is-open { display: flex; }
  .search-input { width: 100%; }
  .desktop-table { display: none; }
  .mobile-doc-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; min-height: 70px; }
  .mobile-doc-card { padding: 11px; border: 1px solid #e4ece9; border-radius: 9px; background: #fff; min-width: 0; }
  .mobile-doc-title { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .mobile-doc-title strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .mobile-doc-meta { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin: 6px 0; font-size: 11px; color: #78878a; }
  .mobile-doc-sub { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; font-size: 11px; color: #93a1a4; }
  .mobile-doc-fields { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: #5c6d71; }
  .mobile-doc-actions { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .mobile-doc-actions :deep(.el-button + .el-button) { margin-left: 0; }
  .pagination-wrapper { justify-content: center; }
}
@media (max-width: 600px) {
  .mobile-doc-list { grid-template-columns: 1fr; }
}
</style>
