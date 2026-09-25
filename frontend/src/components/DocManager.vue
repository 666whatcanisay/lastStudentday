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
            <div class="stat-icon stat-icon-purple"><el-icon><Cpu /></el-icon></div>
            <div class="stat-info">
              <div class="stat-label">OCR 引擎</div>
              <div class="stat-value engine-name">{{ engineLabel }}</div>
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

    <!-- ===== 文档上传区域 ===== -->
    <el-card class="upload-card" shadow="never">
      <template #header>
        <div class="card-header">
          <el-icon><UploadFilled /></el-icon>
          <span>文档上传</span>
          <span class="card-subtitle">支持 JPG / PNG / BMP 图片、PDF 文档（≤50MB，单次最多 20 份）</span>
        </div>
      </template>
      <el-upload
        ref="uploadRef"
        drag
        multiple
        :auto-upload="true"
        :show-file-list="false"
        :http-request="customUpload"
        :limit="20"
        :on-exceed="onExceed"
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

      <!-- 逐份任务的真实状态：等待 / 上传 / 识别 / 成功 / 失败 -->
      <div v-if="uploadTasks.length" class="task-list">
        <div v-for="(t, i) in uploadTasks" :key="i" class="task-item">
          <span class="task-name" :title="t.name">{{ t.name }}</span>
          <el-tag size="small" :type="taskTagType(t.phase)">{{ t.phase }}</el-tag>
          <span v-if="t.msg" class="task-msg">{{ t.msg }}</span>
        </div>
      </div>
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
              style="width: 220px"
              @keyup.enter="loadDocuments"
              @clear="loadDocuments"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-button type="primary" @click="loadDocuments">
              <el-icon><Search /></el-icon>搜索
            </el-button>
            <el-checkbox
              v-model="attachOriginal"
              size="small"
              title="勾选后报告附带原件；原件缺失则该份导出失败（不静默省略）"
            >
              附原件
            </el-checkbox>
            <el-button
              type="warning"
              :loading="exporting"
              :disabled="selectedIds.length === 0"
              @click="exportReportsSelected"
            >
              <el-icon><Document /></el-icon> 导出PDF报告 ({{ selectedIds.length }})
            </el-button>
            <el-button
              type="success"
              :loading="exporting"
              :disabled="selectedIds.length === 0"
              @click="exportSelected"
            >
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
            <span v-if="row.infer_cost">{{ row.infer_cost.toFixed(1) }} ms</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="抽取字段" width="240">
          <template #default="{ row }">
            <div v-if="row.extracts && row.extracts.length > 0" class="extract-preview">
              <el-tag
                v-for="ext in row.extracts.slice(0, 3)"
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
        <el-table-column label="操作" width="230" fixed="right" align="center">
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

    <!-- ========== 详情/编辑对话框：左原件 右字段与全文 ========== -->
    <el-dialog
      v-model="detailVisible"
      :title="currentDoc ? `文档详情 — ${currentDoc.file_name}` : '文档详情'"
      width="88%"
      top="4vh"
      destroy-on-close
    >
      <div v-if="currentDoc" class="detail-wrap">
        <!-- 详情内直接导出当前文档的报告：版本就是这里看到的版本 -->
        <div class="detail-export">
          <el-checkbox v-model="attachOriginal" size="small">附原件</el-checkbox>
          <el-button
            size="small"
            type="warning"
            :loading="exporting"
            :disabled="!currentDoc.current_revision"
            @click="exportCurrentReport"
          >
            <el-icon><Document /></el-icon> 导出本报告 PDF（r{{ currentDoc.current_revision ?? '-' }}）
          </el-button>
        </div>
        <!-- 索引未就绪提示：此时该文档不参与问答，不用旧索引兜底 -->
        <el-alert
          v-if="!indexReady"
          type="info"
          :closable="false"
          show-icon
          class="index-alert"
        >
          <template #title>当前版本的索引尚未就绪，该文档暂不参与智能问答</template>
          <div class="index-alert-body">
            系统会在后台自动重建；也可以
            <el-button v-if="isAdmin" size="small" link type="primary" @click="rebuildIndex">立即重建</el-button>
          </div>
        </el-alert>

        <el-row :gutter="16">
          <!-- ===== 左：原件 ===== -->
          <el-col :span="10" class="preview-col">
            <div class="col-title">
              <span>原件</span>
              <div class="preview-tools">
                <template v-if="currentDoc.file_type === 'pdf'">
                  <el-button size="small" @click="openOriginal">新窗口打开</el-button>
                  <el-button size="small" @click="downloadOriginal">下载</el-button>
                </template>
                <template v-else>
                  <el-button size="small" @click="zoom(-0.2)">缩小</el-button>
                  <el-button size="small" @click="zoom(0.2)">放大</el-button>
                  <el-button size="small" @click="resetZoom">复位</el-button>
                </template>
              </div>
            </div>
            <div class="preview-box">
              <iframe
                v-if="currentDoc.file_type === 'pdf'"
                :src="previewUrl(currentDoc.id)"
                class="preview-frame"
                title="原件预览"
              ></iframe>
              <div v-else class="img-scroll">
                <img
                  :src="previewUrl(currentDoc.id)"
                  :style="{ width: (imageScale * 100) + '%' }"
                  alt="原件预览"
                  @error="onPreviewError"
                />
              </div>
              <el-empty v-if="previewError" description="原件缺失或无法预览" />
            </div>
            <div class="meta-line">
              <el-tag size="small">{{ currentDoc.file_type === 'pdf' ? 'PDF 文档' : '图片' }}</el-tag>
              <el-tag size="small" type="info">版本 r{{ currentDoc.current_revision ?? '-' }}</el-tag>
              <el-tag size="small" :type="currentDoc.reviewed ? 'success' : 'info'">
                {{ currentDoc.reviewed ? '已校对' : '未校对' }}
              </el-tag>
              <el-tag size="small" type="warning" v-if="currentDoc.engine_type">
                引擎 {{ engineText(currentDoc.engine_type) }}
              </el-tag>
            </div>
            <div class="raw-block">
              <div class="block-title">OCR 原始识别文本（不可编辑，人工编辑只改有效全文）</div>
              <pre class="raw-pre">{{ currentDoc.ocr_raw_text || '（暂无识别文本）' }}</pre>
            </div>
          </el-col>

          <!-- ===== 右：字段与有效全文 ===== -->
          <el-col :span="14" class="edit-col">
            <div class="col-title">
              <span>字段与有效全文</span>
              <div class="edit-actions">
                <el-button v-if="!editMode" size="small" type="primary" @click="startEdit">
                  <el-icon><Edit /></el-icon> 编辑
                </el-button>
                <template v-else>
                  <el-button size="small" @click="cancelEdit">取消</el-button>
                  <el-button size="small" type="primary" :loading="saving" @click="saveEdit(false)">
                    保存
                  </el-button>
                  <el-button size="small" type="success" :loading="saving" @click="saveEdit(true)">
                    保存并标为已校对
                  </el-button>
                </template>
                <el-button size="small" @click="openHistory">
                  <el-icon><Clock /></el-icon> 历史
                </el-button>
              </div>
            </div>

            <div class="type-line">
              <span class="label">单据类型</span>
              <el-select
                v-model="editForm.doc_type"
                size="small"
                style="width: 140px"
                :disabled="!editMode"
                @change="onTypeChange"
              >
                <el-option label="发票" value="invoice" />
                <el-option label="合同" value="contract" />
                <el-option label="其他" value="other" />
              </el-select>
              <span class="hint">字段用于统计，请与正文核对</span>
            </div>

            <el-alert
              v-if="typeSwitchHint"
              type="warning"
              :closable="true"
              class="type-hint"
              :title="typeSwitchHint"
            />

            <!-- 字段区 -->
            <div class="fields-grid">
              <div v-for="sp in specItems" :key="sp.key" class="field-item">
                <div class="field-label">{{ sp.label }}</div>
                <el-input
                  v-if="editMode"
                  v-model="editForm.fields[sp.key]"
                  size="small"
                  :placeholder="sp.kind === 'amount' ? '金额，最多两位小数' : (sp.kind === 'date' ? 'YYYY-MM-DD' : '留空表示未识别')"
                />
                <div v-else class="field-static" :class="{ 'field-empty': !fieldValue(sp.key) }">
                  {{ fieldValue(sp.key) || '未识别' }}
                </div>
              </div>
            </div>

            <!-- 有效全文 -->
            <div class="text-block">
              <div class="block-title">
                有效全文（统计/索引/导出使用）
                <span v-if="editMode" class="hint">编辑全文不会自动重抽字段</span>
              </div>
              <el-input
                v-if="editMode"
                v-model="editForm.effective_text"
                type="textarea"
                :rows="12"
                resize="vertical"
              />
              <pre v-else class="raw-pre effective-pre">{{ currentDoc.effective_text || '（暂无有效文本）' }}</pre>
            </div>

            <div v-if="currentDoc.last_attempt_error" class="error-box">
              <el-alert
                type="error"
                :closable="false"
                :title="'最近一次识别尝试失败：' + currentDoc.last_attempt_error"
              />
            </div>
          </el-col>
        </el-row>
      </div>
    </el-dialog>

    <!-- ========== 版本历史抽屉（只读） ========== -->
    <el-drawer v-model="historyVisible" title="版本历史（只读）" size="45%">
      <div v-if="revisions.length === 0" class="text-muted">暂无版本记录</div>
      <el-timeline v-else>
        <el-timeline-item
          v-for="rv in revisions"
          :key="rv.revision"
          :timestamp="formatTime(rv.created_at)"
          placement="top"
          :type="rv.is_current ? 'primary' : 'info'"
        >
          <el-card shadow="never" class="rev-card">
            <div class="rev-head">
              <b>r{{ rv.revision }}</b>
              <el-tag size="small" :type="reasonTag(rv.change_reason)">
                {{ reasonText(rv.change_reason) }}
              </el-tag>
              <el-tag size="small" type="info">{{ docTypeLabel(rv.doc_type) }}</el-tag>
              <el-tag v-if="rv.is_current" size="small" type="success">当前</el-tag>
              <el-tag v-if="rv.is_reviewed" size="small" type="success">已校对</el-tag>
            </div>
            <div class="rev-actions">
              <el-button size="small" link @click="viewRevision(rv.revision)">查看完整快照</el-button>
            </div>
          </el-card>
        </el-timeline-item>
      </el-timeline>

      <el-divider v-if="activeRevision" content-position="left">r{{ activeRevision.revision }} 快照</el-divider>
      <div v-if="activeRevision" class="snapshot">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="变更原因">{{ reasonText(activeRevision.change_reason) }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ docTypeLabel(activeRevision.doc_type) }}</el-descriptions-item>
          <el-descriptions-item label="引擎">{{ engineText(activeRevision.engine_type) }}</el-descriptions-item>
          <el-descriptions-item label="时间">{{ formatTime(activeRevision.created_at) }}</el-descriptions-item>
        </el-descriptions>
        <div class="block-title">字段</div>
        <div class="fields-grid">
          <div v-for="f in activeRevision.fields" :key="f.field_key" class="field-item">
            <div class="field-label">{{ f.field_name }}</div>
            <div class="field-static">{{ f.field_value || '未识别' }}</div>
          </div>
        </div>
        <div class="block-title">有效全文</div>
        <pre class="raw-pre">{{ activeRevision.effective_text || '（空）' }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getHealth,
  getDocumentStats,
  getDocumentList,
  getDocumentDetail,
  patchDocument,
  getDocumentRevisions,
  getDocumentRevision,
  getFieldSpecs,
  recognizeOCR,
  reRecognizeOCR,
  uploadFile,
  deleteDocument,
  rebuildRagIndex,
  previewUrl,
  exportExcel,
  exportReport,
  exportReports,
  saveBlob,
} from '../api/index.js'

defineProps({ isAdmin: { type: Boolean, default: true } })

// ==================== 响应式数据 ====================
const loading = ref(false)
const documents = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const searchKeyword = ref('')
const selectedIds = ref([])
const uploadRef = ref()
// 导出：attachOriginal 控制是否附原件；exporting 防止重复点击
const attachOriginal = ref(false)
const exporting = ref(false)

// 上传任务（逐份排队，状态真实）
const uploadTasks = ref([])

// 详情/编辑
const detailVisible = ref(false)
const currentDoc = ref(null)
const editMode = ref(false)
const saving = ref(false)
const editForm = reactive({ doc_type: 'other', effective_text: '', fields: {} })
let originalFields = {}
const specItems = ref([])
const typeSwitchHint = ref('')
const previewError = ref(false)
const imageScale = ref(1)

// 历史
const historyVisible = ref(false)
const revisions = ref([])
const activeRevision = ref(null)

// optimized: null = 尚未取到引擎信息（未知）。不要用 false 作默认值 ——
// 那样首屏会先把「未知」误显示成「模拟引擎 (Mock)」闪一下。
const ocrEngine = reactive({ name: '', description: '', optimized: null, engine_type: null })
const stats = reactive({ total: 0, success: 0, avgCost: '0.0' })

const engineLabel = computed(() => {
  if (!ocrEngine.engine_type) return '检测中'
  return { rapidocr: 'RapidOCR', tesseract: 'Tesseract', mock: '模拟引擎' }[ocrEngine.engine_type] || ocrEngine.engine_type
})

const indexReady = computed(() => {
  const d = currentDoc.value
  if (!d || d.current_revision == null) return false
  return d.index_status === 1 && d.indexed_revision === d.current_revision
})

// ==================== 工具函数 ====================
const statusText = (status) => ({ 0: '待识别', 1: '识别成功', 2: '识别失败' }[status] || '未知')
const statusTagType = (status) => ({ 0: 'info', 1: 'success', 2: 'danger' }[status] || 'info')
const docTypeLabel = (t) => ({ invoice: '发票', contract: '合同', other: '其他' }[t] || '其他')
const docTypeTag = (t) => ({ invoice: 'danger', contract: 'warning', other: 'info' }[t] || 'info')
const reasonText = (r) => ({ ocr: 'OCR 识别', edit: '人工编辑', retype: '切换类型', legacy: '旧库迁移' }[r] || r)
const reasonTag = (r) => ({ ocr: 'primary', edit: 'warning', retype: 'warning', legacy: 'info' }[r] || 'info')
const engineText = (e) => ({ rapidocr: 'RapidOCR', tesseract: 'Tesseract', mock: '模拟', legacy_unknown: '历史未知' }[e] || e || '未知')
const taskTagType = (p) => ({ '等待': 'info', '上传': 'primary', '识别': 'warning', '成功': 'success', '重复': 'warning', '失败': 'danger' }[p] || 'info')

function formatTime(time) {
  if (!time) return '-'
  const d = new Date(time)
  if (isNaN(d.getTime())) return time
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fieldValue(key) {
  const hit = (currentDoc.value?.extracts || []).find(e => e.field_key === key)
  return hit ? hit.field_value : ''
}

/** 取出后端错误里的中文提示（拦截器已处理网络/超时的映射） */
function errMsg(e) {
  return e?.msg || e?.response?.data?.msg || e?.message || '未知原因'
}

// 上传进度只显示真实字节百分比；任务阶段之间不编造进度条
function zoom(delta) {
  imageScale.value = Math.min(3, Math.max(0.4, +(imageScale.value + delta).toFixed(2)))
}
function resetZoom() { imageScale.value = 1 }
function onPreviewError() { previewError.value = true }
function openOriginal() { window.open(previewUrl(currentDoc.value.id), '_blank') }
function downloadOriginal() { window.open(previewUrl(currentDoc.value.id, true), '_blank') }

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
    // 拦截器已提示
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
    const eng = res.data.ocr_engine || {}
    ocrEngine.name = eng.name || ''
    ocrEngine.description = eng.description || ''
    ocrEngine.optimized = eng.optimized ?? null
    ocrEngine.engine_type = eng.engine_type || (eng.optimized ? 'rapidocr' : null)
  } catch (e) {
    // ignore
  }
}

// ==================== 上传（统一 axios + 串行识别） ====================
// 为什么要串行：识别是 CPU 密集的单进程任务，同时跑 N 份只会让每份都变慢，
// 且会与索引重建争抢资源。上传可以并发，识别必须排队。
const taskQueue = []
let queueRunning = false

function enqueue(fn) {
  taskQueue.push(fn)
  if (queueRunning) return
  queueRunning = true
  ;(async () => {
    while (taskQueue.length) {
      const job = taskQueue.shift()
      try { await job() } catch (e) { console.error(e) }
    }
    queueRunning = false
  })()
}

function onExceed() {
  ElMessage.warning('单次最多选择 20 份文件，请分批上传')
}

async function customUpload(options) {
  const { file } = options
  const task = reactive({ name: file.name, phase: '等待', msg: '' })
  uploadTasks.value.push(task)

  enqueue(async () => {
    task.phase = '上传'
    try {
      const res = await uploadFile(file, (e) => {
        if (e.total) task.msg = `${Math.round((e.loaded / e.total) * 100)}%`
      })
      const data = res.data || {}

      // 内容去重：同内容不同名不新增；命中回收站提示恢复
      if (data.duplicate) {
        task.phase = '重复'
        task.msg = data.in_recycle_bin ? '内容相同，原件在回收站，可恢复' : '内容相同，未重复创建'
        ElMessage.info(task.msg)
        return
      }

      const docId = data.doc_id
      task.phase = '识别'
      task.msg = '逐页处理中，请耐心等待'
      const rr = await recognizeOCR(docId)
      task.phase = '成功'
      task.msg = `耗时 ${rr.data?.infer_cost?.toFixed?.(0) ?? '-'} ms`
    } catch (e) {
      task.phase = '失败'
      task.msg = errMsg(e)
    } finally {
      loadDocuments()
      loadStats()
    }
  })
}

// ==================== 详情 / 编辑 ====================
async function viewDetail(row, edit = false) {
  try {
    const res = await getDocumentDetail(row.id)
    currentDoc.value = res.data
    previewError.value = false
    imageScale.value = 1
    typeSwitchHint.value = ''
    await loadSpecs(currentDoc.value.doc_type || 'other')
    resetEditForm()
    editMode.value = edit
    detailVisible.value = true
  } catch (e) {
    ElMessage.error(`获取详情失败：${errMsg(e)}`)
  }
}

async function loadSpecs(docType) {
  try {
    const res = await getFieldSpecs(docType)
    specItems.value = res.data.items || []
  } catch (e) {
    specItems.value = []
  }
}

function resetEditForm() {
  const d = currentDoc.value
  editForm.doc_type = d.doc_type || 'other'
  editForm.effective_text = d.effective_text || ''
  editForm.fields = {}
  originalFields = {}
  for (const sp of specItems.value) {
    const v = fieldValue(sp.key) ?? ''
    editForm.fields[sp.key] = v
    originalFields[sp.key] = v
  }
}

function startEdit() {
  resetEditForm()
  editMode.value = true
}

function cancelEdit() {
  resetEditForm()
  editMode.value = false
  typeSwitchHint.value = ''
}

/**
 * 切换类型：一次界面确认 + 列出目标类型字段供编辑，**不在切换瞬间提交**。
 * 原类型字段只保留在历史里；公共字段（币种）由后端沿用。
 */
async function onTypeChange(newType) {
  if (newType === (currentDoc.value.doc_type || 'other')) return
  try {
    await ElMessageBox.confirm(
      '切换类型后：目标类型的字段会从当前有效全文重新抽取，'
      + '原类型字段只保留在历史版本中；公共字段（如币种）会沿用。'
      + '保存前你可以先修改新字段。',
      '确认切换单据类型',
      { type: 'warning', confirmButtonText: '确认切换', cancelButtonText: '取消' }
    )
  } catch {
    // 用户取消 → 回滚选择器
    editForm.doc_type = currentDoc.value.doc_type || 'other'
    return
  }
  await loadSpecs(newType)
  editForm.fields = {}
  originalFields = {}
  for (const sp of specItems.value) {
    editForm.fields[sp.key] = ''
    originalFields[sp.key] = ''
  }
  editMode.value = true
  typeSwitchHint.value =
    `已切换到「${docTypeLabel(newType)}」：字段将在保存时从有效全文重新抽取，`
    + '也可在下方直接填写后保存（留空的字段会被保存为未识别）。'
}

async function saveEdit(markReviewed) {
  if (!currentDoc.value) return
  saving.value = true
  try {
    // 只提交**发生过变化**的字段：留空代表"清空该字段"，
    // 未改动的字段不提交，避免把后端重抽结果覆盖成空。
    const fields = {}
    for (const [key, val] of Object.entries(editForm.fields)) {
      const cur = (val ?? '').trim()
      if (cur !== (originalFields[key] ?? '')) fields[key] = cur
    }

    const res = await patchDocument(currentDoc.value.id, {
      expected_revision: currentDoc.value.current_revision,
      doc_type: editForm.doc_type,
      effective_text: editForm.effective_text,
      fields,
      mark_reviewed: !!markReviewed,
    })

    const data = res.data || {}
    if (data.revision_created) {
      ElMessage.success(`已发布新版本 r${data.new_revision}`)
    } else {
      ElMessage.info('无实际变化，未创建新版本')
    }
    // 重新载入详情，拿到最新版本号与字段
    const fresh = await getDocumentDetail(currentDoc.value.id)
    currentDoc.value = fresh.data
    await loadSpecs(currentDoc.value.doc_type || 'other')
    resetEditForm()
    editMode.value = false
    typeSwitchHint.value = ''
    loadDocuments()
    loadStats()
  } catch (e) {
    if (e?.code === 409 || e?.response?.status === 409) {
      try {
        await ElMessageBox.alert(
          '这份文档已被其他操作修改（版本已变化）。请重新载入后再编辑，本次修改未保存。',
          '版本冲突',
          { confirmButtonText: '重新载入', type: 'warning' }
        )
      } catch { /* 关闭也走重新载入 */ }
      const fresh = await getDocumentDetail(currentDoc.value.id)
      currentDoc.value = fresh.data
      await loadSpecs(currentDoc.value.doc_type || 'other')
      resetEditForm()
      editMode.value = false
      loadDocuments()
    } else {
      ElMessage.error(`保存失败：${errMsg(e)}`)
    }
  } finally {
    saving.value = false
  }
}

async function rebuildIndex() {
  try {
    await rebuildRagIndex([currentDoc.value.id])
    ElMessage.success('已触发索引重建')
    const fresh = await getDocumentDetail(currentDoc.value.id)
    currentDoc.value = fresh.data
  } catch (e) {
    ElMessage.error(`重建索引失败：${errMsg(e)}`)
  }
}

// ==================== 重新识别 ====================
async function reRecognize(row) {
  try {
    await ElMessageBox.confirm(
      '重新识别会基于原件生成新版本，覆盖当前有效结果（历史版本仍可查看）。确认继续？',
      '确认重新识别',
      { type: 'warning', confirmButtonText: '确认重新识别', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  const notif = `正在重新识别「${row.file_name}」，请耐心等待`
  ElMessage.info(notif)
  try {
    await reRecognizeOCR(row.id)
    ElMessage.success('重新识别完成，已发布新版本')
    loadDocuments()
    loadStats()
  } catch (e) {
    ElMessage.error(`重新识别失败：${errMsg(e)}`)
    loadDocuments()
  }
}

// ==================== 历史 ====================
async function openHistory() {
  historyVisible.value = true
  activeRevision.value = null
  try {
    const res = await getDocumentRevisions(currentDoc.value.id)
    revisions.value = res.data.items || []
  } catch (e) {
    revisions.value = []
    ElMessage.error(`读取历史失败：${errMsg(e)}`)
  }
}

async function viewRevision(rev) {
  try {
    const res = await getDocumentRevision(currentDoc.value.id, rev)
    activeRevision.value = res.data
  } catch (e) {
    ElMessage.error(`读取快照失败：${errMsg(e)}`)
  }
}

// ==================== 导出与删除 ====================
/**
 * 统一的导出执行器：负责「进行中不重复点击 / 超时明确提示 / 失败可重试」这三条。
 *
 * 为什么不用 window.open(url)：
 *   那种方式拿不到响应 —— 后端返回 JSON 错误时浏览器会直接把错误文本显示或存成文件，
 *   用户看到的是一个打不开的「PDF」。走 axios 才能识别 Content-Type 并给出中文提示。
 */
async function runExport(fn, label) {
  if (exporting.value) {
    ElMessage.warning('导出正在进行中，请等待完成')
    return
  }
  exporting.value = true
  try {
    const res = await fn()
    const blob = res.data
    if (!blob || blob.size === 0) {
      ElMessage.error(`${label}失败：服务端返回了空文件`)
      return
    }
    saveBlob(blob, res.filename)
    ElMessage.success(`${label}完成：${res.filename || ''}`)
  } catch (e) {
    // 拦截器已把 JSON 错误、超时、断网都翻成中文提示并 reject，这里只补一句可重试
    if (e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')) {
      ElMessage.error(`${label}超时：文档较多或原件较大，请减少份数后重试`)
    }
  } finally {
    exporting.value = false
  }
}

async function exportSelected() {
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请先选择要导出的文档')
    return
  }
  await runExport(
    () => exportExcel(selectedIds.value.slice()),
    `导出Excel（${selectedIds.value.length} 份）`)
}

async function exportReportsSelected() {
  const ids = selectedIds.value.slice()
  if (ids.length === 0) {
    ElMessage.warning('请先选择要导出的文档')
    return
  }
  if (ids.length > 20) {
    ElMessage.warning('单次最多导出 20 份，请分批选择')
    return
  }
  if (attachOriginal.value) {
    try {
      await ElMessageBox.confirm(
        '勾选了「附原件」：任一份原件缺失都会导致该份导出失败（不会静默省略附录），失败原因会记在 ZIP 清单里。继续？',
        '附原件导出', { type: 'warning' })
    } catch (e) {
      return
    }
  }
  // 一份也走批量接口：行为一致，且同样带清单
  await runExport(
    () => exportReports(ids, attachOriginal.value),
    `导出PDF报告（${ids.length} 份）`)
}

async function exportCurrentReport() {
  if (!currentDoc.value?.id) return
  await runExport(
    () => exportReport(currentDoc.value.id, attachOriginal.value),
    '导出报告')
}

async function handleDelete(row) {
  try {
    await deleteDocument(row.id)
    ElMessage.success('已移入回收站（可在数据管理中恢复）')
    loadDocuments()
    loadStats()
  } catch (e) {
    ElMessage.error(`删除失败：${errMsg(e)}`)
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

defineExpose({ refresh: () => { loadDocuments(); loadStats() } })
</script>

<style scoped>
/* ========== 统计卡片 ========== */
.stat-row { margin-bottom: 16px; }
.stat-card { border-radius: 8px; }
.stat-content { display: flex; align-items: center; gap: 16px; }
.stat-icon {
  width: 48px; height: 48px; border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 24px; color: #fff;
}
.stat-icon-blue { background: linear-gradient(135deg, #409EFF, #36cfc9); }
.stat-icon-green { background: linear-gradient(135deg, #67C23A, #85ce61); }
.stat-icon-orange { background: linear-gradient(135deg, #E6A23C, #f5da55); }
.stat-icon-purple { background: linear-gradient(135deg, #9b59b6, #c39bd3); }
.stat-info { flex: 1; }
.stat-label { font-size: 13px; color: #909399; }
.stat-value { font-size: 24px; font-weight: 600; color: #303133; }
.stat-value.engine-name { font-size: 18px; }
.stat-unit { font-size: 13px; color: #909399; }

/* ========== 卡片通用 ========== */
.card-header { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
.card-subtitle { font-size: 12px; color: #909399; margin-left: 12px; }
.header-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }

/* ========== 上传区 ========== */
.upload-card { margin-bottom: 16px; border-radius: 8px; }
.upload-card .el-upload-dragger { padding: 26px 0; border-radius: 8px; }
.task-list { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.task-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.task-name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-msg { color: #909399; font-size: 12px; }

/* ========== 列表区 ========== */
.list-card { border-radius: 8px; }
.pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 16px; }
.extract-preview { display: flex; flex-wrap: wrap; gap: 4px; }
.extract-tag { font-size: 12px; }
.rev-text { font-size: 12px; color: #606266; margin-right: 6px; }
.review-tag { transform: scale(0.9); }

/* ========== 详情：左原件 右字段 ========== */
.detail-wrap { padding: 2px; }
.detail-export {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed #ebeef5;
}
.index-alert { margin-bottom: 12px; }
.index-alert-body { font-size: 13px; }
.col-title {
  display: flex; align-items: center; justify-content: space-between;
  font-weight: 600; font-size: 14px; margin-bottom: 8px;
}
.preview-tools { display: flex; gap: 6px; }
.preview-box {
  border: 1px solid #ebeef5; border-radius: 8px; background: #fafafa;
  height: 460px; overflow: auto; display: flex; align-items: flex-start; justify-content: center;
}
.preview-frame { width: 100%; height: 100%; border: 0; }
.img-scroll { width: 100%; }
.img-scroll img { display: block; margin: 0 auto; }
.meta-line { display: flex; gap: 6px; margin: 8px 0; flex-wrap: wrap; }

.raw-block { margin-top: 10px; }
.block-title { font-size: 13px; color: #606266; margin: 8px 0 6px; font-weight: 600; }
.raw-pre {
  background: #f5f7fa; padding: 12px; border-radius: 8px;
  max-height: 220px; overflow: auto; font-size: 13px; line-height: 1.7;
  white-space: pre-wrap; margin: 0;
}
.effective-pre { max-height: 320px; }

.type-line { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.type-line .label { font-size: 13px; color: #606266; }
.type-line .hint { font-size: 12px; color: #909399; margin-left: 8px; }
.type-hint { margin-bottom: 8px; }

.fields-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.field-item { background: #fafafa; border: 1px solid #ebeef5; border-radius: 8px; padding: 8px 10px; }
.field-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.field-static { font-size: 14px; font-weight: 600; color: #303133; word-break: break-all; }
.field-empty { color: #C0C4CC; font-weight: 400; }
.text-block { margin-top: 12px; }
.error-box { margin-top: 12px; }

/* ========== 历史 ========== */
.rev-card { border-radius: 8px; }
.rev-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rev-actions { margin-top: 6px; }
.snapshot { margin-top: 8px; }

.text-muted { color: #909399; }
</style>
