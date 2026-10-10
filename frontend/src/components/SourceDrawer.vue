<template>
  <!--
    来源抽屉 —— 「溯源」的最后一跳。

    引用卡片上写着「[1] 示例发票 · 第1页」，但用户凭什么相信？
    这里把该引用对应的内容摊开，答案可被验证，幻觉才无处藏身。

    ⚠️ 两种来源必须**分开表述**（本轮修复的核心）：
      · source_kind = 'fields' —— 结构化「字段摘要」。它只是若干字段名:值，
        既不是完整原文，也没有字符偏移。旧文案把它写成「已展示完整原文」，
        属于**误导**：用户以为看到了原件全文，其实只是一小段字段列表。
      · source_kind = 'text'   —— 正文引用。有 char_start/char_end 才做高亮；
        没有偏移时才退回展示该版本的完整有效文本。

    定位依据：document_chunk.char_start / char_end —— 阶段一分块时就存好了，
    指向该版本快照（document_revision）。这是当初为界面预留的字段，现在正好用上。
  -->
  <el-drawer
    :model-value="modelValue"
    :size="'52%'"
    :with-header="false"
    @update:model-value="$emit('update:modelValue', $event)"
    @opened="scrollToHit"
  >
    <div class="src-wrap">
      <!-- 头部：引用序号 + 文件名 + 前后切换 -->
      <div class="src-head">
        <div class="sh-left">
          <span class="sh-index">[{{ refItem?.index }}]</span>
          <span class="sh-name">{{ refItem?.file_name }}</span>
          <el-tag v-if="refItem?.page_no" size="small" type="info" effect="plain">第{{ refItem.page_no }}页</el-tag>
          <el-tag v-if="isFieldRef" size="small" type="success" effect="plain">字段摘要</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">正文</el-tag>
          <el-tag v-if="refItem && !refItem.cited && showCited" size="small" type="info" effect="plain">未被引用</el-tag>
        </div>
        <div class="sh-right">
          <el-button-group>
            <el-button size="small" :disabled="!hasPrev" @click="$emit('prev')">
              <el-icon><ArrowLeft /></el-icon>
            </el-button>
            <el-button size="small" :disabled="!hasNext" @click="$emit('next')">
              <el-icon><ArrowRight /></el-icon>
            </el-button>
          </el-button-group>
          <el-button size="small" text @click="$emit('update:modelValue', false)">关闭</el-button>
        </div>
      </div>

      <!-- 双路溯源信息条 -->
      <div v-if="refItem" class="src-meta">
        <el-tag v-if="refItem.vec_rank != null" size="small" type="primary" effect="plain">
          向量召回 #{{ refItem.vec_rank }}（相似度 {{ fmt(refItem.vec_score) }}）
        </el-tag>
        <el-tag v-if="refItem.bm25_rank != null" size="small" type="warning" effect="plain">
          关键词召回 #{{ refItem.bm25_rank }}（BM25 {{ fmt(refItem.bm25_score) }}）
        </el-tag>
        <el-tag size="small" type="success" effect="plain">融合得分 {{ fmt(refItem.score) }}</el-tag>
        <!-- 字符偏移只对正文引用有意义；字段摘要没有坐标，绝不显示偏移 -->
        <span v-if="hitRange" class="src-range">
          高亮区间：第 {{ hitRange[0] }} ~ {{ hitRange[1] }} 字符
        </span>
      </div>

      <div v-loading="loading" class="src-body">
        <el-alert v-if="notice" :title="notice" type="warning" :closable="false" show-icon />

        <!-- ========== 视图 A：字段摘要（只列该版本中确实有值的字段） ========== -->
        <template v-if="isFieldRef && viewMode === 'fields'">
          <div class="src-kindbar">
            <span class="skb-title">字段摘要</span>
            <el-button
              size="small" text type="primary"
              :disabled="!snapshotText"
              @click="viewMode = 'text'"
            >查看完整有效文本 →</el-button>
          </div>
          <div v-if="valuedFields.length" class="field-list">
            <div v-for="f in valuedFields" :key="f.field_key || f.field_name" class="field-row">
              <span class="fr-name">{{ f.field_name }}</span>
              <span class="fr-value">{{ f.field_value }}</span>
            </div>
          </div>
          <el-empty v-else description="该版本没有已识别出值的字段" :image-size="70" />
          <el-alert
            type="info" :closable="false" show-icon class="src-alert"
            title="「字段摘要」只列出该版本中已识别出值的字段。留空表示未从原件中确认到该字段，不代表原件没有。「已校对」仅表示该版本已被人工确认，不代表每个字段都有值。"
          />
        </template>

        <!-- ========== 视图 B：正文（高亮命中；无偏移则展示该版本完整有效文本） ========== -->
        <template v-else-if="snapshotText">
          <div v-if="isFieldRef" class="src-kindbar">
            <span class="skb-title">完整有效文本</span>
            <el-button size="small" text type="primary" @click="viewMode = 'fields'">← 返回字段摘要</el-button>
          </div>
          <pre v-if="hitRange" class="src-text"><span>{{ snapshotText.slice(0, hitRange[0]) }}</span><mark ref="markEl" class="src-mark">{{ snapshotText.slice(hitRange[0], hitRange[1]) }}</mark><span>{{ snapshotText.slice(hitRange[1]) }}</span></pre>
          <pre v-else class="src-text">{{ snapshotText }}</pre>
          <el-alert
            v-if="!hitRange && !isFieldRef"
            type="info" :closable="false" show-icon
            title="该片段没有可用的字符偏移信息，已展示该版本的完整有效文本"
            class="src-alert"
          />
          <el-alert
            v-else-if="!hitRange && isFieldRef"
            type="info" :closable="false" show-icon
            title="这是该版本的完整有效文本（校对后的正文），与上面的「字段摘要」不是同一份内容。"
            class="src-alert"
          />
        </template>

        <el-empty v-else-if="!loading" description="未获取到内容" :image-size="70" />
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { getDocumentDetail, getDocumentRevision } from '../api/index.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  refItem: { type: Object, default: null },
  hasPrev: { type: Boolean, default: false },
  hasNext: { type: Boolean, default: false },
  /** 降级场景下 cited 全为 false 属「无判定者」，不该标成「未被引用」 */
  showCited: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue', 'prev', 'next'])

const snapshotText = ref('')      // 该版本的有效全文（校对后的正文）
const snapshotFields = ref([])    // 该版本的结构化字段
const loading = ref(false)
const notice = ref('')
const viewMode = ref('text')      // 'fields' | 'text'
let requestSequence = 0
const markEl = ref(null)

/** 是否结构化字段来源（决定文案与是否显示偏移高亮） */
const isFieldRef = computed(() => props.refItem?.source_kind === 'fields')

/** 只保留该版本中确实有值的字段 —— 空字段不列出来冒充「摘要」 */
const valuedFields = computed(() =>
  (snapshotFields.value || []).filter(
    (f) => f && f.field_value != null && String(f.field_value).trim() !== ''
  )
)

/**
 * 计算高亮区间。
 * 必须做边界钳制 —— char_start/char_end 存的是分块时的偏移，
 * 若文档后来被重新识别（文本长度变化），偏移可能越界，直接 slice 会切出错位内容。
 * 字段来源没有坐标，永远返回 null。
 */
const hitRange = computed(() => {
  const r = props.refItem
  if (!r || r.source_kind === 'fields' || r.char_start == null || r.char_end == null || !snapshotText.value) return null
  const len = snapshotText.value.length
  const s = Math.max(0, Math.min(r.char_start, len))
  const e = Math.max(s, Math.min(r.char_end, len))
  return e > s ? [s, e] : null
})

watch(
  () => [props.modelValue, props.refItem?.doc_id, props.refItem?.revision, props.refItem?.chunk_id],
  async ([visible]) => {
    if (!visible || !props.refItem) return
    if (props.refItem.doc_id === undefined || props.refItem.doc_id === null) return
    loading.value = true
    snapshotText.value = ''
    snapshotFields.value = []
    notice.value = ''
    // 字段来源默认展示「字段摘要」；正文来源只能看正文
    viewMode.value = props.refItem.source_kind === 'fields' ? 'fields' : 'text'
    const ticket = ++requestSequence
    const reference = { ...props.refItem }
    try {
      const current = (await getDocumentDetail(reference.doc_id)).data
      if (ticket !== requestSequence) return
      if (current.deleted_at) {
        notice.value = '该文档已删除，此引用不再有效。'
        return
      }
      if (reference.revision == null) {
        notice.value = '旧引用缺少版本信息，请重新提问。'
        return
      }
      const snapshot = (await getDocumentRevision(reference.doc_id, reference.revision)).data
      if (ticket !== requestSequence) return
      const kindLabel = reference.source_kind === 'fields' ? '字段摘要' : '校对后的正文'
      notice.value = reference.revision !== current.current_revision
        ? `旧版本 r${reference.revision}（当前 r${current.current_revision}），不代表当前结果。`
        : `引用版本 r${reference.revision} · ${kindLabel}`
      snapshotFields.value = snapshot.fields || []
      snapshotText.value = snapshot.effective_text || ''
    } catch (e) {
      if (ticket === requestSequence) { snapshotText.value = ''; snapshotFields.value = []; notice.value = '引用已不可用，请重新提问。' }
    } finally {
      if (ticket === requestSequence) loading.value = false
      nextTick(scrollToHit)
    }
  }
)

/** 打开后把高亮位置滚进视野 —— 否则用户看到的是原文开头，高亮在屏幕外 */
function scrollToHit() {
  nextTick(() => {
    if (markEl.value?.scrollIntoView) {
      markEl.value.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  })
}

function fmt(v) {
  if (v == null) return '-'
  return v < 1 ? v.toFixed(4) : v.toFixed(2)
}
</script>

<style scoped>
.src-wrap {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.src-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 0 12px;
  border-bottom: 1px solid #ebeef5;
  flex-shrink: 0;
}
.sh-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.sh-index {
  font-weight: 700;
  color: #409eff;
}
.sh-name {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sh-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.src-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 10px 0;
  flex-shrink: 0;
}
.src-range {
  font-size: 12px;
  color: #909399;
  margin-left: auto;
}
.src-body {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  min-height: 0;
}
.src-kindbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 10px 0 8px;
}
.skb-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}
.field-list {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;
  background: #fff;
}
.field-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 9px 14px;
  border-bottom: 1px solid #f2f6fc;
}
.field-row:last-child {
  border-bottom: none;
}
.fr-name {
  flex-shrink: 0;
  width: 88px;
  font-size: 13px;
  color: #909399;
}
.fr-value {
  font-size: 13px;
  color: #303133;
  word-break: break-all;
  font-weight: 500;
}
.src-text {
  font-family: ui-monospace, Consolas, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.9;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-all;
  background: #fafbfc;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 14px 16px;
  margin: 0;
}
/* 高亮样式：黄色底 + 左侧竖条，既要醒目又不要刺眼 */
.src-mark {
  background: #fff3cd;
  border-left: 3px solid #e6a23c;
  padding: 2px 1px;
  border-radius: 2px;
  font-weight: 600;
  color: #8a6d3b;
  scroll-margin-top: 20px;
}
.src-alert {
  margin-top: 10px;
}
</style>
