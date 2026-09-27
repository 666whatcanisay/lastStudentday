<template>
    <!-- ========== 详情/编辑对话框：左原件 右字段与全文 ========== -->
    <el-dialog
      v-model="detailVisible"
      class="detail-dialog"
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
          <el-col :xs="24" :sm="24" :md="10" class="preview-col">
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
          <el-col :xs="24" :sm="24" :md="14" class="edit-col">
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
    <el-drawer v-model="historyVisible" title="版本历史（只读）" size="min(560px, 100vw)">
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
</template>

<script setup>
import { ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDocumentDetail, patchDocument, getDocumentRevisions,
  getDocumentRevision, getFieldSpecs, rebuildRagIndex, previewUrl,
  exportReport, saveBlob } from '../api/index.js'
import { notifyDocumentsChanged } from '../documentEvents.js'

defineProps({ isAdmin: { type: Boolean, default: false } })
const attachOriginal = ref(false)
const exporting = ref(false)
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


const indexReady = computed(() => {
  const d = currentDoc.value
  if (!d || d.current_revision == null) return false
  return d.index_status === 1 && d.indexed_revision === d.current_revision
})


const docTypeLabel = (t) => ({ invoice: '发票', contract: '合同', other: '其他' }[t] || '其他')
const reasonText = (r) => ({ ocr: 'OCR 识别', edit: '人工编辑', retype: '切换类型', legacy: '旧库迁移' }[r] || r)
const reasonTag = (r) => ({ ocr: 'primary', edit: 'warning', retype: 'warning', legacy: 'info' }[r] || 'info')
const engineText = (e) => ({ rapidocr: 'RapidOCR', tesseract: 'Tesseract', mock: '模拟', legacy_unknown: '历史未知' }[e] || e || '未知')

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

// 原件只在详情中加载，列表保持轻量。
function zoom(delta) {
  imageScale.value = Math.min(3, Math.max(0.4, +(imageScale.value + delta).toFixed(2)))
}
function resetZoom() { imageScale.value = 1 }
function onPreviewError() { previewError.value = true }
function openOriginal() { window.open(previewUrl(currentDoc.value.id), '_blank') }
function downloadOriginal() { window.open(previewUrl(currentDoc.value.id, true), '_blank') }


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
    notifyDocumentsChanged()
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
      notifyDocumentsChanged()
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

// ==================== 单份报告导出 ====================
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

async function exportCurrentReport() {
  if (!currentDoc.value?.id) return
  await runExport(
    () => exportReport(currentDoc.value.id, attachOriginal.value),
    '导出报告')
}


defineExpose({ open: viewDetail })
</script>

<style scoped>
/* ========== 详情：左原件 右字段 ========== */
.detail-wrap { padding: 2px; }
.detail-export {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px dashed #ebeef5;
}
.index-alert { margin-bottom: 12px; }
.index-alert-body { font-size: 13px; }
.col-title {
  display: flex; align-items: center; justify-content: space-between; gap: 7px; flex-wrap: wrap;
  font-weight: 600; font-size: 14px; margin-bottom: 8px;
}
.preview-tools, .edit-actions { display: flex; gap: 5px; flex-wrap: wrap; }
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

@media (max-width: 767px) {
  .preview-box { height: 300px; }
  .edit-col { margin-top: 16px; }
  .type-line { flex-wrap: wrap; }
  .type-line .hint { margin-left: 0; }
  .fields-grid { grid-template-columns: 1fr; }
  .raw-pre { max-height: 160px; }
}
</style>
