<template>
  <!--
    原文抽屉 —— 「溯源」的最后一跳。

    引用卡片上写着「[1] 曹操出行发票 · 第1页」，但用户凭什么相信？
    这里把该文档的 OCR 原文完整摊开，并把命中的字符区间高亮出来。
    答案可被验证，幻觉才无处藏身。

    定位依据：document_chunk.char_start / char_end —— 阶段一分块时就存好了，
    指向 document_info.ocr_raw_text。这是当初为界面预留的字段，现在正好用上。
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
        <span v-if="hitRange" class="src-range">
          高亮区间：第 {{ hitRange[0] }} ~ {{ hitRange[1] }} 字符
        </span>
      </div>

      <!-- 原文 -->
      <div v-loading="loading" class="src-body">
        <template v-if="text">
          <pre v-if="hitRange" class="src-text"><span>{{ text.slice(0, hitRange[0]) }}</span><mark ref="markEl" class="src-mark">{{ text.slice(hitRange[0], hitRange[1]) }}</mark><span>{{ text.slice(hitRange[1]) }}</span></pre>
          <pre v-else class="src-text">{{ text }}</pre>
          <el-alert
            v-if="!hitRange"
            type="info" :closable="false" show-icon
            title="该文档没有可用的字符偏移信息，已展示完整原文"
            class="src-alert"
          />
        </template>
        <el-empty v-else-if="!loading" description="未获取到 OCR 原文" :image-size="70" />
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { getDocumentDetail } from '../api/index.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  refItem: { type: Object, default: null },
  hasPrev: { type: Boolean, default: false },
  hasNext: { type: Boolean, default: false },
  /** 降级场景下 cited 全为 false 属「无判定者」，不该标成「未被引用」 */
  showCited: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue', 'prev', 'next'])

const text = ref('')
const loading = ref(false)
const markEl = ref(null)

/**
 * 计算高亮区间。
 * 必须做边界钳制 —— char_start/char_end 存的是分块时的偏移，
 * 若文档后来被重新识别（文本长度变化），偏移可能越界，直接 slice 会切出错位内容。
 */
const hitRange = computed(() => {
  const r = props.refItem
  if (!r || r.char_start == null || r.char_end == null || !text.value) return null
  const len = text.value.length
  const s = Math.max(0, Math.min(r.char_start, len))
  const e = Math.max(s, Math.min(r.char_end, len))
  return e > s ? [s, e] : null
})

watch(
  () => [props.modelValue, props.refItem?.chunk_id],
  async ([visible]) => {
    if (!visible || !props.refItem) return
    if (props.refItem.doc_id === undefined || props.refItem.doc_id === null) return
    loading.value = true
    text.value = ''
    try {
      const res = await getDocumentDetail(props.refItem.doc_id)
      text.value = res.data?.ocr_raw_text || ''
    } catch (e) {
      text.value = ''
    } finally {
      loading.value = false
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
  min-height: 0;
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
