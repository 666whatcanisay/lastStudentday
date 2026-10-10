<template>
  <!--
    引用卡片 —— 本界面的「溯源」入口。

    两种视觉状态刻意拉开差距：
      · cited = true  —— 模型真的引用了它，正常显示 + 左侧主色竖条
      · cited = false —— 召回了但没被引用，整体淡化 + 虚线边框
    目的是让用户一眼看出「答案其实只用了这几条」，不被一堆引用淹没。

    showCited=false 用于**降级场景**（检索为空 / LLM 未配置 / LLM 调用失败）：
    此时根本没有模型去引用任何东西，cited 全为 false 是「没有判断者」而不是
    「判断为不相关」。继续淡化会让用户误以为这些片段不相关 —— 恰恰相反，
    它们是本次唯一可看的内容。故降级时关闭该视觉区分。
  -->
  <div
    class="citation-card"
    :class="{ 'is-uncited': uncited, 'is-active': active }"
    @click="$emit('open', refItem)"
  >
    <div class="cc-head">
      <span class="cc-index">[{{ refItem.index }}]</span>
      <span class="cc-name" :title="refItem.file_name">{{ refItem.file_name || ('文档 ' + refItem.doc_id) }}</span>
      <el-tag v-if="refItem.page_no" size="small" type="info" effect="plain" class="cc-page">
        第{{ refItem.page_no }}页
      </el-tag>
    </div>

    <div class="cc-snippet">{{ refItem.snippet || '（无片段）' }}</div>

    <div class="cc-foot">
      <!-- 双路溯源徽标：让用户看见「这条是哪一路召回的」 -->
      <el-tag v-if="sourceTag.type" size="small" :type="sourceTag.type" effect="plain" class="cc-badge">
        {{ sourceTag.text }}
      </el-tag>
      <span v-if="refItem.vec_score != null" class="cc-score">向量 {{ refItem.vec_score.toFixed(3) }}</span>
      <span v-if="refItem.bm25_score != null" class="cc-score">关键词 {{ refItem.bm25_score.toFixed(2) }}</span>
      <!-- 字段来源不是「原文」，措辞必须区分，否则用户以为能看到原件全文 -->
      <span class="cc-open">{{ refItem.source_kind === 'fields' ? '查看字段摘要 →' : '查看原文 →' }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  refItem: { type: Object, required: true },
  active: { type: Boolean, default: false },
  /** 是否展示「被引用 / 未被引用」的视觉区分（降级场景置 false） */
  showCited: { type: Boolean, default: true },
})
defineEmits(['open'])

/** 仅在「有模型做过引用判定」且未被引用时才淡化 */
const uncited = computed(() => props.showCited && !props.refItem.cited)

/** 双路溯源：两路都召回 / 仅向量 / 仅关键词 */
const sourceTag = computed(() => {
  const { vec_rank: v, bm25_rank: b } = props.refItem
  if (v != null && b != null) return { text: `两路召回 · 向量#${v} 关键词#${b}`, type: 'success' }
  if (v != null) return { text: `仅向量召回 · #${v}`, type: 'primary' }
  if (b != null) return { text: `仅关键词召回 · #${b}`, type: 'warning' }
  return { text: '', type: '' }
})
</script>

<style scoped>
.citation-card {
  border: 1px solid #dcdfe6;
  border-left: 3px solid #409eff;
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
  background: #fff;
  transition: all 0.18s;
  min-width: 0;
}
.citation-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.18);
  transform: translateY(-1px);
}
.citation-card.is-active {
  border-color: #409eff;
  background: #ecf5ff;
}
/* 未被引用的条目：淡化处理，视觉上明显弱于被引用的 */
.citation-card.is-uncited {
  border-left-color: #c0c4cc;
  border-style: dashed;
  background: #fafafa;
  opacity: 0.62;
}
.citation-card.is-uncited:hover {
  opacity: 1;
  border-style: solid;
}

.cc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 5px;
}
.cc-index {
  font-weight: 600;
  color: #409eff;
  font-size: 13px;
  flex-shrink: 0;
}
.is-uncited .cc-index {
  color: #909399;
}
.cc-name {
  font-size: 12px;
  color: #303133;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.cc-page {
  flex-shrink: 0;
  transform: scale(0.88);
}
.cc-snippet {
  font-size: 12px;
  color: #606266;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-bottom: 6px;
  word-break: break-all;
}
.cc-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cc-badge {
  transform: scale(0.86);
  transform-origin: left center;
}
.cc-score {
  font-size: 11px;
  color: #909399;
  font-family: ui-monospace, Consolas, monospace;
}
.cc-open {
  font-size: 11px;
  color: #409eff;
  margin-left: auto;
  opacity: 0;
  transition: opacity 0.18s;
}
.citation-card:hover .cc-open {
  opacity: 1;
}
</style>
