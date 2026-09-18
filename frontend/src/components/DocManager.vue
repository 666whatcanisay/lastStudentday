<template>
  <!--
    文档管理页签。

    这块内容原本整个躺在 App.vue 里（646 行）。加入智能问答界面后 App.vue 会突破
    1500 行，无法维护，因此按「页签 = 组件」拆分。
    本组件内容与拆分前的 App.vue 主体**逐行等价**，只去掉了外层的 el-main 包裹。
  -->
  <div class="doc-manager">
    <!-- ===== 统计卡片 ===== -->
    <el-row :gutter="16" class="stat-row">
      <el-col :span="6">
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
      <el-col :span="6">
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
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-icon stat-icon-orange"><el-icon><Clock /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">平均推理耗时</div>
              <div class="stat-value">{{ stats.avgCost }}<span class="stat-unit">ms</span></div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-icon stat-icon-purple"><el-icon><DataAnalysis /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">轻量化优化</div>
              <div class="stat-value">{{ ocrEngine.optimized ? '已启用' : '未启用' }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ===== 引擎降级提示 =====
         用全局横幅而不是逐条标记：引擎模式本来就是**全局**状态。
         逐条标记需要给 DocumentInfo 加「引擎模式」字段 + 数据库迁移，
         而本项目只有 create_all()（只建表不改表），本轮未引入 Alembic，故不做。 -->
    <el-alert
      v-if="ocrEngine.optimized === false"
      class="mock-alert"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    >
      <template #title>当前为模拟引擎 (Mock)，识别结果是占位文本</template>
      <div>
        RapidOCR 未能加载，系统已降级为模拟引擎。下表里标记为「识别成功」的文档，
        其识别文本均为<b>占位内容，并非真实识别结果</b>，导出的 Excel 同样如此。
        请检查后端依赖是否完整（需要 pyclipper / Shapely / PyYAML / onnxruntime）。
      </div>
    </el-alert>

    <!-- ===== 文档上传区域 ===== -->
    <el-card class="upload-card" shadow="never">
      <template #header>
        <div class="card-header">
          <el-icon><UploadFilled /></el-icon>
          <span>文档上传</span>
          <span class="card-subtitle">支持 JPG / PNG / BMP 图片、PDF 文档（≤50MB）</span>
        </div>
      </template>
      <el-upload
        ref="uploadRef"
        drag
        multiple
        :auto-upload="true"
        :show-file-list="false"
        :http-request="customUpload"
        accept=".jpg,.jpeg,.png,.bmp,.pdf"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">将文件拖到此处，或<em>点击上传</em></div>
        <template #tip>
          <div class="el-upload__tip">
            上传后自动执行「图像预处理 → OCR识别 → 结构化字段抽取」。
            <b>多页 PDF 逐页处理，10 页约需 40 秒</b>，请耐心等待、勿关闭页面。
          </div>
        </template>
      </el-upload>
    </el-card>

    <!-- ===== 文档列表区域 ===== -->
    <el-card class="list-card" shadow="never">
      <template #header>
        <div class="card-header">
          <el-icon><Document /></el-icon>
          <span>文档管理</span>
          <div class="header-actions">
            <el-input
              v-model="searchKeyword"
              placeholder="搜索文件名或识别内容"
              clearable
              style="width: 260px"
              @keyup.enter="loadDocuments"
              @clear="loadDocuments"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-button type="primary" @click="loadDocuments">
              <el-icon><Search /></el-icon>搜索
            </el-button>
            <el-button type="success" @click="exportSelected" :disabled="selectedIds.length === 0">
              <el-icon><Download /></el-icon> 导出Excel ({{ selectedIds.length }})
            </el-button>
            <el-button @click="loadDocuments">
              <el-icon><Refresh /></el-icon>刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        :data="documents"
        v-loading="loading"
        @selection-change="handleSelectionChange"
        stripe
        style="width: 100%"
      >
        <el-table-column type="selection" width="45" />
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="file_name" label="文件名" min-width="180" show-overflow-tooltip />
        <el-table-column prop="file_type" label="类型" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.file_type === 'pdf' ? 'danger' : 'primary'" size="small">
              {{ row.file_type === 'pdf' ? 'PDF' : '图片' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="infer_cost" label="推理耗时" width="100" align="center">
          <template #default="{ row }">
            <span v-if="row.infer_cost">{{ row.infer_cost.toFixed(1) }} ms</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="抽取字段" width="280">
          <template #default="{ row }">
            <div v-if="row.extracts && row.extracts.length > 0" class="extract-preview">
              <el-tag
                v-for="ext in row.extracts"
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
        <el-table-column prop="upload_time" label="上传时间" width="160" align="center">
          <template #default="{ row }">{{ formatTime(row.upload_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="viewDetail(row)">
              <el-icon><View /></el-icon> 详情
            </el-button>
            <el-button
              v-if="row.status !== 1"
              size="small"
              type="warning"
              link
              @click="reRecognize(row)"
            >
              <el-icon><Refresh /></el-icon> 识别
            </el-button>
            <el-popconfirm title="确定删除此文档？" @confirm="handleDelete(row)">
              <template #reference>
                <el-button size="small" type="danger" link>
                  <el-icon><Delete /></el-icon> 删除
                </el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @size-change="loadDocuments"
          @current-change="loadDocuments"
        />
      </div>
    </el-card>

    <!-- ========== 文档详情对话框 ========== -->
    <el-dialog v-model="detailVisible" title="文档识别详情" width="80%" top="5vh" destroy-on-close>
      <div v-if="currentDoc" class="detail-content">
        <el-descriptions :column="3" border class="detail-desc">
          <el-descriptions-item label="文档ID">{{ currentDoc.id }}</el-descriptions-item>
          <el-descriptions-item label="文件名">{{ currentDoc.file_name }}</el-descriptions-item>
          <el-descriptions-item label="文件类型">{{ currentDoc.file_type === 'pdf' ? 'PDF文档' : '图片' }}</el-descriptions-item>
          <el-descriptions-item label="识别状态">
            <el-tag :type="statusTagType(currentDoc.status)" size="small">{{ statusText(currentDoc.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="推理耗时">
            <span v-if="currentDoc.infer_cost" class="cost-highlight">{{ currentDoc.infer_cost.toFixed(2) }} ms</span>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="上传时间">{{ formatTime(currentDoc.upload_time) }}</el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">结构化抽取字段</el-divider>
        <el-row :gutter="16">
          <el-col :span="8" v-for="ext in currentDoc.extracts" :key="ext.id">
            <el-card shadow="hover" class="field-card">
              <div class="field-name">{{ ext.field_name }}</div>
              <div class="field-value" :class="{ 'field-empty': !ext.field_value }">
                {{ ext.field_value || '未识别到' }}
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-divider content-position="left">OCR 原始识别文本</el-divider>
        <div class="raw-text-area">
          <pre v-if="currentDoc.ocr_raw_text">{{ currentDoc.ocr_raw_text }}</pre>
          <el-empty v-else description="暂无识别文本" />
        </div>

        <div v-if="currentDoc.error_msg" class="error-box">
          <el-alert type="error" :title="'识别失败: ' + currentDoc.error_msg" :closable="false" />
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElNotification } from 'element-plus'
import {
  getHealth,
  getDocumentStats,
  getDocumentList,
  getDocumentDetail,
  recognizeOCR,
  deleteDocument,
} from '../api/index.js'

// ==================== 响应式数据 ====================
const loading = ref(false)
const documents = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchKeyword = ref('')
const selectedIds = ref([])
const uploadRef = ref()

const detailVisible = ref(false)
const currentDoc = ref(null)

// optimized: null = 尚未取到引擎信息（未知）。不要用 false 作默认值 ——
// 那样首屏会先把「未知」误显示成「模拟引擎 (Mock)」闪一下。
const ocrEngine = reactive({ name: '', description: '', optimized: null })
const stats = reactive({ total: 0, success: 0, avgCost: '0.0' })

// ==================== 工具函数 ====================
// 后端 status 是数字: 0=待识别, 1=识别成功, 2=识别失败
const statusText = (status) => {
  return { 0: '待识别', 1: '识别成功', 2: '识别失败' }[status] || '未知'
}
const statusTagType = (status) => {
  return { 0: 'info', 1: 'success', 2: 'danger' }[status] || 'info'
}

function formatTime(time) {
  if (!time) return '-'
  // 处理后端返回的 datetime 字符串（可能带T或不带）
  const d = new Date(time)
  if (isNaN(d.getTime())) return time
  const Y = d.getFullYear()
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${Y}-${M}-${D} ${h}:${m}:${s}`
}

// ==================== 数据加载 ====================
async function loadDocuments() {
  loading.value = true
  try {
    const res = await getDocumentList({
      page: currentPage.value,
      page_size: pageSize.value,
      keyword: searchKeyword.value || undefined,
    })
    documents.value = res.data.items
    total.value = res.data.total
  } catch (e) {
    // 拦截器已处理
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    const res = await getDocumentStats()
    if (res.data) {
      stats.total = res.data.total
      stats.success = res.data.success
      stats.avgCost = res.data.avg_cost || '0.0'
    }
  } catch (e) {
    console.error('加载统计失败', e)
  }
}

async function fetchHealth() {
  try {
    const res = await getHealth()
    ocrEngine.name = res.data.ocr_engine.name
    ocrEngine.description = res.data.ocr_engine.description
    ocrEngine.optimized = res.data.ocr_engine.optimized
  } catch (e) {
    // ignore
  }
}

// ==================== 文件上传 ====================
async function customUpload(options) {
  const { file } = options
  const formData = new FormData()
  formData.append('file', file)

  const notif = ElNotification({
    title: '上传中',
    message: `${file.name} 正在上传...`,
    type: 'info',
    duration: 0,
  })

  try {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/file/upload')

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const percent = Math.round((e.loaded / e.total) * 100)
        notif.message = `${file.name} 上传中 ${percent}%`
      }
    }

    xhr.onload = () => {
      notif.close()
      const res = JSON.parse(xhr.responseText)
      if (res.code === 200) {
        ElNotification({
          title: '上传成功',
          message: `${file.name} 上传成功，正在执行OCR识别...`,
          type: 'success',
          duration: 3000,
        })
        // 自动触发 OCR 识别
        recognizeDoc(res.data.doc_id, file.name)
      } else {
        ElNotification({ title: '上传失败', message: res.msg, type: 'error', duration: 5000 })
      }
    }

    xhr.onerror = () => {
      notif.close()
      ElNotification({ title: '上传失败', message: `${file.name} 网络错误`, type: 'error' })
    }

    xhr.send(formData)
  } catch (e) {
    notif.close()
    ElNotification({ title: '上传失败', message: e.message, type: 'error' })
  }
}

// ==================== OCR 识别 ====================
async function recognizeDoc(docId, fileName) {
  const notif = ElNotification({
    title: 'OCR识别中',
    message: `${fileName || '文档'} 正在识别...（多页 PDF 逐页处理，10 页约需 40 秒，请勿关闭页面）`,
    type: 'info',
    duration: 0,
  })

  try {
    const res = await recognizeOCR(docId)
    notif.close()
    ElNotification({
      title: '识别成功',
      message: `${fileName || '文档'} 识别完成，耗时 ${res.data.infer_cost?.toFixed(1)}ms`,
      type: 'success',
      duration: 4000,
    })
    loadDocuments()
    loadStats()
  } catch (e) {
    notif.close()
    // 带上真实原因。原来这里是硬编码的「OCR识别失败」，
    // 用户完全不知道是超时、文件损坏还是服务端出错。
    const reason = e?.response?.data?.msg || e?.message || '未知原因'
    ElNotification({
      title: '识别失败',
      message: `${fileName || '文档'}：${reason}`,
      type: 'error',
      duration: 6000,
    })
    loadDocuments()
  }
}

async function reRecognize(row) {
  recognizeDoc(row.id, row.file_name)
}

// ==================== 详情查看 ====================
async function viewDetail(row) {
  try {
    const res = await getDocumentDetail(row.id)
    currentDoc.value = res.data
    detailVisible.value = true
  } catch (e) {
    ElMessage.error('获取详情失败')
  }
}

// ==================== Excel 导出 ====================
async function exportSelected() {
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请先选择要导出的文档')
    return
  }
  const ids = selectedIds.value.join(',')
  // 使用 window.open 方式导出，更可靠
  window.open(`/api/document/export?doc_ids=${ids}`, '_blank')
  ElMessage.success(`正在导出 ${selectedIds.value.length} 条文档数据`)
}

// ==================== 删除 ====================
async function handleDelete(row) {
  try {
    await deleteDocument(row.id)
    ElMessage.success('删除成功')
    loadDocuments()
    loadStats()
  } catch (e) {
    ElMessage.error('删除失败')
  }
}

function handleSelectionChange(rows) {
  selectedIds.value = rows.map(r => r.id)
}

// ==================== 初始化 ====================
onMounted(() => {
  fetchHealth()
  loadDocuments()
  loadStats()
})

// 供父组件（切换到本页签时）刷新数据
defineExpose({ refresh: () => { loadDocuments(); loadStats() } })
</script>

<style scoped>
/* ========== 统计卡片 ========== */
.stat-row { margin-bottom: 16px; }
.stat-card { border-radius: 8px; }
.stat-content {
  display: flex;
  align-items: center;
  gap: 16px;
}
.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  color: #fff;
}
.stat-icon-blue { background: linear-gradient(135deg, #409EFF, #36cfc9); }
.stat-icon-green { background: linear-gradient(135deg, #67C23A, #85ce61); }
.stat-icon-orange { background: linear-gradient(135deg, #E6A23C, #f5da55); }
.stat-icon-purple { background: linear-gradient(135deg, #9b59b6, #c39bd3); }
.stat-info { flex: 1; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 24px; font-weight: 600; color: #303133; }
.stat-unit { font-size: 13px; color: #909399; }

/* ========== 卡片通用 ========== */
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 15px;
}
.card-subtitle {
  font-size: 12px;
  color: #909399;
  margin-left: 12px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

/* ========== 上传区 ========== */
.upload-card { margin-bottom: 16px; border-radius: 8px; }
.upload-card .el-upload-dragger { padding: 30px 0; border-radius: 8px; }

/* ========== 列表区 ========== */
.list-card { border-radius: 8px; }
.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.extract-preview { display: flex; flex-wrap: wrap; gap: 4px; }
.extract-tag { font-size: 12px; }

/* ========== 详情对话框 ========== */
.detail-content { padding: 4px; }
.detail-desc { margin-bottom: 16px; }
.cost-highlight { font-weight: 600; color: #409EFF; }
.field-card { margin-bottom: 12px; border-radius: 8px; }
.field-name { font-size: 12px; color: #909399; margin-bottom: 4px; }
.field-value { font-size: 16px; font-weight: 600; color: #303133; }
.field-empty { color: #C0C4CC; }
.raw-text-area {
  background: #f5f7fa;
  padding: 16px;
  border-radius: 8px;
  max-height: 300px;
  overflow-y: auto;
}
.raw-text-area pre {
  font-size: 13px;
  line-height: 1.8;
  white-space: pre-wrap;
  margin: 0;
}
.error-box { margin-top: 16px; }

.text-muted { color: #909399; }
</style>
