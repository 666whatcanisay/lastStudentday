<template>
  <section class="upload-panel" aria-label="上传并识别文档">
    <el-upload
      drag multiple :auto-upload="true" :show-file-list="false"
      :http-request="customUpload" :limit="20" :on-exceed="onExceed"
      accept=".jpg,.jpeg,.png,.bmp,.pdf"
    >
      <div class="upload-inline">
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-copy">
          <strong>上传并识别文档</strong>
          <span>拖入文件或点击选择 · 图片/PDF · 单份 ≤ 50 MB，最多 20 份</span>
        </div>
        <span class="upload-action">选择文件</span>
      </div>
    </el-upload>
    <p class="upload-note">上传后依次预处理、OCR 识别和抽取字段；多页 PDF 可能需要较长时间，请保持页面打开。</p>
    <div v-if="tasks.length" class="task-list" aria-live="polite">
      <div v-for="task in tasks" :key="task.id" class="task-item">
        <span class="task-name" :title="task.name">{{ task.name }}</span>
        <el-tag size="small" :type="tagType(task.phase)">{{ task.phase }}</el-tag>
        <span class="task-msg">{{ task.msg }}</span>
      </div>
    </div>
  </section>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { uploadFile, recognizeOCR } from '../api/index.js'
import { notifyDocumentsChanged } from '../documentEvents.js'

const tasks = ref([])
const queue = []
let running = false
let nextId = 0

const tagType = phase => ({ '等待': 'info', '上传': 'primary', '识别': 'warning',
  '成功': 'success', '重复': 'warning', '失败': 'danger' }[phase] || 'info')

function onExceed() {
  ElMessage.warning('单次最多选择 20 份文件，请分批上传')
}

function enqueue(job) {
  queue.push(job)
  if (running) return
  running = true
  ;(async () => {
    while (queue.length) {
      try { await queue.shift()() } catch (error) { console.error(error) }
    }
    running = false
  })()
}

function customUpload({ file }) {
  const task = reactive({ id: ++nextId, name: file.name, phase: '等待', msg: '' })
  tasks.value.push(task)
  enqueue(async () => {
    task.phase = '上传'
    try {
      const result = await uploadFile(file, event => {
        if (event.total) task.msg = `${Math.round(event.loaded / event.total * 100)}%`
      })
      const data = result.data || {}
      if (data.duplicate) {
        task.phase = '重复'
        task.msg = data.in_recycle_bin ? '内容相同，原件在回收站，可恢复' : '内容相同，未重复创建'
        ElMessage.info(task.msg)
        return
      }
      task.phase = '识别'
      task.msg = '逐页处理中，请耐心等待'
      const recognition = await recognizeOCR(data.doc_id)
      task.phase = '成功'
      task.msg = `耗时 ${recognition.data?.infer_cost?.toFixed?.(0) ?? '-'} ms`
    } catch (error) {
      task.phase = '失败'
      task.msg = error?.msg || error?.response?.data?.msg || error?.message || '未知错误'
    } finally {
      notifyDocumentsChanged()
    }
  })
}
</script>

<style scoped>
.upload-panel { margin-bottom: 14px; }
.upload-panel :deep(.el-upload), .upload-panel :deep(.el-upload-dragger) { width: 100%; }
.upload-panel :deep(.el-upload-dragger) { padding: 13px 18px; border-radius: 10px; background: #fff; }
.upload-inline { display: flex; align-items: center; gap: 14px; text-align: left; }
.upload-icon { font-size: 26px; color: #286e79; }
.upload-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 3px; }
.upload-copy strong { font-size: 14px; color: #19343d; }
.upload-copy span, .upload-note, .task-msg { color: #6d7e82; font-size: 12px; }
.upload-action { color: #286e79; font-weight: 600; white-space: nowrap; }
.upload-note { margin: 5px 3px 0; }
.task-list { display: grid; gap: 5px; margin-top: 8px; }
.task-item { display: flex; align-items: center; gap: 8px; padding: 6px 9px; background: #fff; border: 1px solid #e7eeed; border-radius: 8px; font-size: 12px; }
.task-name { min-width: 0; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 600px) {
  .upload-panel :deep(.el-upload-dragger) { padding: 11px; }
  .upload-inline { gap: 9px; }
  .upload-copy span { display: none; }
  .upload-note { line-height: 1.5; }
  .task-item { flex-wrap: wrap; }
}
</style>
