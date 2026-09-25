<template>
  <!--
    数据管理页签（方案 B §3.6）

    三块内容刻意放在同一页，因为它们互相依赖、分开看容易出事：
      1. 回收站：软删除的文档在这里，可恢复或永久删除（仅此页可永久删除）
      2. 备份：手动一致性备份，写入门禁会暂停新变更等待在途写入
      3. 恢复说明：**不提供运行中切库接口**，恢复必须走受控停服务脚本

    界面上的路径全部是项目内相对路径（后端只返回相对路径），
    不在页面上暴露本机绝对目录结构。
  -->
  <div class="data-manager">
    <!-- ===== 回收站 ===== -->
    <el-card shadow="never" class="card">
      <template #header>
        <div class="card-header">
          <el-icon><Delete /></el-icon>
          <span>回收站</span>
          <span class="card-subtitle">
            软删除的文档不参与列表/统计/导出/问答；原件与版本历史都保留，恢复后重新校验索引
          </span>
          <el-button size="small" @click="loadRecycle" style="margin-left: auto">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
        </div>
      </template>

      <el-table :data="recycleItems" v-loading="recycleLoading" stripe size="small" style="width: 100%">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="file_name" label="文件名" min-width="180" show-overflow-tooltip />
        <el-table-column label="单据类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small">{{ docTypeLabel(row.doc_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本/校对" width="120" align="center">
          <template #default="{ row }">
            <span>r{{ row.current_revision ?? '-' }}</span>
            <el-tag size="small" :type="row.reviewed ? 'success' : 'info'" class="tag-gap">
              {{ row.reviewed ? '已校对' : '未校对' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="删除时间" width="170" align="center">
          <template #default="{ row }">{{ formatTime(row.deleted_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="190" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click="restore(row)">
              <el-icon><RefreshLeft /></el-icon> 恢复
            </el-button>
            <el-button size="small" type="danger" link @click="permanentDelete(row)">
              <el-icon><Delete /></el-icon> 永久删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!recycleLoading && recycleItems.length === 0"
                description="回收站是空的" :image-size="60" />
      <el-pagination
        v-if="recycleTotal > recyclePageSize"
        class="pager"
        layout="prev, pager, next, total"
        :total="recycleTotal"
        :page-size="recyclePageSize"
        :current-page="recyclePage"
        @current-change="onRecyclePage"
      />
      <div class="hint">
        回收站无限期保留，不自动清空；永久删除仅对回收站内的文档生效，且只删除本应用存储目录内的原件。
      </div>
    </el-card>

    <!-- ===== 备份 ===== -->
    <el-card shadow="never" class="card">
      <template #header>
        <div class="card-header">
          <el-icon><Download /></el-icon>
          <span>备份</span>
          <span class="card-subtitle">SQLite Online Backup + 稳定原件 → 一致性 ZIP（含回收站/历史/问答记录）</span>
        </div>
      </template>

      <el-alert type="warning" :closable="false" show-icon class="alert-gap">
        <template #title>备份未加密，且包含全部文档内容与问答记录</template>
        <div>
          请下载后另存到其他磁盘，不要放在共享目录或云盘；备份文件不包含 .env/Key、运行日志、模型与虚拟环境。
        </div>
      </el-alert>

      <div class="backup-actions">
        <el-button type="primary" :loading="backingUp" :disabled="backingUp" @click="doBackup">
          <el-icon><Download /></el-icon>
          {{ backingUp ? '正在备份（等待在途写入结束）...' : '立即备份并下载' }}
        </el-button>
        <span class="hint inline">
          备份期间新的写入会被暂时挡下，等待最多 60 秒；超时会取消本次备份并提示稍后重试（不会强杀任务）。
        </span>
      </div>
      <div class="path-line">
        备份输出目录：<code>{{ info.backup_dir || 'data/backups' }}</code>
        <el-button size="small" text type="primary" @click="copy(info.backup_dir)">复制路径</el-button>
      </div>
    </el-card>

    <!-- ===== 恢复说明 ===== -->
    <el-card shadow="never" class="card">
      <template #header>
        <div class="card-header">
          <el-icon><WarningFilled /></el-icon>
          <span>恢复说明（受控停服务恢复）</span>
          <span class="card-subtitle">本项目不提供运行中切库的在线接口</span>
        </div>
      </template>

      <el-descriptions :column="2" border size="small" class="desc">
        <el-descriptions-item label="恢复脚本">{{ info.script || 'scripts/restore_backup.py' }}</el-descriptions-item>
        <el-descriptions-item label="数据根目录">{{ info.data_root || '项目根目录（默认）' }}</el-descriptions-item>
        <el-descriptions-item label="应用版本">v{{ info.app_version || '-' }}</el-descriptions-item>
        <el-descriptions-item label="schema 版本">{{ info.schema_version ?? '-' }}</el-descriptions-item>
      </el-descriptions>

      <div class="cmd-line">
        <code>{{ info.command || '' }}</code>
        <el-button size="small" text type="primary" @click="copy(info.command)">复制命令</el-button>
      </div>

      <el-steps direction="vertical" :active="0" class="steps">
        <el-step v-for="(s, i) in (info.steps || [])" :key="i" :title="s" />
      </el-steps>

      <el-alert v-for="(w, i) in (info.warnings || [])" :key="'w' + i"
                type="warning" :closable="false" show-icon :title="w" class="alert-gap" />

      <div class="hint">{{ info.data_root_note || '' }}</div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getDocumentList, restoreDocument, permanentDeleteDocument,
  createBackup, getRestoreInfo, saveBlob,
} from '../api/index.js'

const recycleItems = ref([])
const recycleTotal = ref(0)
const recyclePage = ref(1)
const recyclePageSize = ref(10)
const recycleLoading = ref(false)
const backingUp = ref(false)
const info = reactive({})

const docTypeLabel = (t) => ({ invoice: '发票', contract: '合同', other: '其他' }[t] || '其他')

function formatTime(time) {
  if (!time) return '-'
  const d = new Date(time)
  if (isNaN(d.getTime())) return time
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function errMsg(e) {
  return e?.msg || e?.response?.data?.msg || e?.message || '未知原因'
}

async function loadRecycle() {
  recycleLoading.value = true
  try {
    const res = await getDocumentList({
      in_recycle_bin: true,
      page: recyclePage.value,
      page_size: recyclePageSize.value,
    })
    recycleItems.value = res.data.items
    recycleTotal.value = res.data.total
  } catch (e) {
    // 拦截器已提示
  } finally {
    recycleLoading.value = false
  }
}

function onRecyclePage(p) {
  recyclePage.value = p
  loadRecycle()
}

async function restore(row) {
  try {
    await restoreDocument(row.id)
    ElMessage.success('已恢复；系统会重新校验该文档的索引')
    loadRecycle()
  } catch (e) {
    ElMessage.error(`恢复失败：${errMsg(e)}`)
  }
}

async function permanentDelete(row) {
  try {
    await ElMessageBox.confirm(
      `永久删除「${row.file_name}」：将同时删除数据库记录、全部版本快照与原件，**不可恢复**。` +
      `确定继续？`,
      '永久删除确认', { type: 'error', confirmButtonText: '永久删除', cancelButtonText: '取消' })
  } catch (e) {
    return
  }
  try {
    await permanentDeleteDocument(row.id)
    ElMessage.success('已永久删除')
    loadRecycle()
  } catch (e) {
    // 后端在"原件删除失败/路径越界"时不返回完全成功，这里必须如实提示
    ElMessage.error(`永久删除未完成：${errMsg(e)}`)
  }
}

async function doBackup() {
  if (backingUp.value) {
    ElMessage.warning('备份正在进行，请等待完成')
    return
  }
  backingUp.value = true
  try {
    const res = await createBackup()
    const blob = res.data
    if (!blob || blob.size === 0) {
      ElMessage.error('备份失败：服务端返回了空文件')
      return
    }
    saveBlob(blob, res.filename)
    ElMessage.success(`备份完成：${res.filename || ''}（请另存到其他磁盘）`)
  } catch (e) {
    // 409 = 写入门禁等待超时，后端要求"稍后重试"，不自动重试以免叠加任务
    if (e?.code === 409) {
      ElMessage.warning(`本次备份已取消：${errMsg(e)}`)
    } else if (e?.code === 'ECONNABORTED' || /timeout/i.test(e?.message || '')) {
      ElMessage.error('备份超时：数据量较大，请稍后重试或先关闭其他任务')
    }
  } finally {
    backingUp.value = false
  }
}

function copy(text) {
  if (!text) return
  const doCopy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
      ElMessage.success('已复制')
    } catch (e) {
      ElMessage.warning('复制失败，请手动选择文本复制')
    }
  }
  doCopy()
}

async function loadInfo() {
  try {
    const res = await getRestoreInfo()
    Object.assign(info, res.data)
  } catch (e) {
    // 拦截器已提示
  }
}

onMounted(() => {
  loadRecycle()
  loadInfo()
})

defineExpose({ refresh: () => { loadRecycle(); loadInfo() } })
</script>

<style scoped>
.data-manager { padding-bottom: 8px; }
.card { margin-bottom: 14px; }
.card-header { display: flex; align-items: center; gap: 8px; font-weight: 600; }
.card-subtitle { font-weight: 400; font-size: 12px; color: #909399; }
.tag-gap { margin-left: 6px; }
.pager { margin-top: 10px; }
.hint { font-size: 12px; color: #909399; margin-top: 10px; line-height: 1.7; }
.hint.inline { margin-top: 0; margin-left: 10px; }
.alert-gap { margin-bottom: 10px; }
.backup-actions { display: flex; align-items: center; flex-wrap: wrap; margin-top: 4px; }
.path-line { font-size: 12px; color: #606266; margin-top: 10px; }
.desc { margin-bottom: 10px; }
.cmd-line {
  display: flex; align-items: center; gap: 8px;
  background: #f5f7fa; border-radius: 4px; padding: 6px 10px; margin-bottom: 12px;
}
.cmd-line code { font-size: 12px; color: #303133; }
.steps { margin-bottom: 10px; }
code { font-family: Consolas, monospace; }
</style>
