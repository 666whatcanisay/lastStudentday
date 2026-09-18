<template>
  <!--
    检索过程面板 —— 本界面「可解释性」的载体。

    为什么常驻而不是折叠：这是个教学演示系统，双路检索的互补性、查询改写的作用、
    RRF 融合的效果，正是最值得展示的东西。折叠起来就成了藏在细节里的卖点。

    面板按链路顺序分四段，每段对应检索链路的一个环节：
      ① 多轮指代消解（仅有历史时出现）
      ② 查询改写（相对时间解析 + 同义扩展）
      ③ 双路召回（含每条的向量/关键词名次与融合得分）
      ④ 本次统计
  -->
  <div class="trace-panel">
    <div class="trace-title">
      <el-icon><Compass /></el-icon>
      <span>检索过程</span>
      <el-tag v-if="stage" size="small" type="warning" effect="plain" class="stage-tag">{{ stage }}</el-tag>
    </div>

    <!-- 空态：还没提问过 -->
    <el-empty v-if="!result && !loading" description="提个问题，这里会显示检索过程" :image-size="60" />

    <div v-else class="trace-body">
      <!-- ① 多轮指代消解 -->
      <div v-if="coref && coref.changed" class="trace-block">
        <div class="tb-head"><span class="tb-num">①</span>多轮指代消解</div>
        <div class="tb-row">
          <span class="tb-key">代词</span>
          <span class="tb-val">{{ coref.pronoun ? '「' + coref.pronoun + '」' : '省略式追问' }}</span>
        </div>
        <div class="tb-row">
          <span class="tb-key">指向</span>
          <span class="tb-val" :title="coref.focus && coref.focus.file_name">
            {{ (coref.focus && coref.focus.file_name) || '-' }}
          </span>
        </div>
        <div class="tb-row">
          <span class="tb-key">消解后</span>
          <span class="tb-val mono">{{ coref.resolved }}</span>
        </div>
        <el-tag size="small" type="info" effect="plain" class="tb-method">
          {{ coref.method === 'llm' ? 'LLM 消解' : '规则消解' }}
        </el-tag>
      </div>

      <!-- ② 查询改写 -->
      <div v-if="rewrite" class="trace-block">
        <div class="tb-head"><span class="tb-num">{{ coref && coref.changed ? '②' : '①' }}</span>查询改写</div>
        <div v-if="rewrite.time_scope" class="tb-row">
          <span class="tb-key">时间</span>
          <span class="tb-val">
            {{ rewrite.time_scope.raw }} → <b>{{ rewrite.time_scope.label }}</b>
          </span>
        </div>
        <div class="tb-row">
          <span class="tb-key">扩展</span>
          <span class="tb-val">
            <template v-if="rewrite.expansions && rewrite.expansions.length">
              <el-tag
                v-for="t in rewrite.expansions" :key="t"
                size="small" effect="plain" class="kw-tag"
              >{{ t }}</el-tag>
            </template>
            <span v-else class="tb-muted">无（问题已含语料术语）</span>
          </span>
        </div>
        <div class="tb-row">
          <span class="tb-key">向量查询</span>
          <span class="tb-val mono">{{ rewrite.vec_query }}</span>
        </div>
        <div class="tb-row">
          <span class="tb-key">关键词查询</span>
          <span class="tb-val mono">{{ rewrite.bm25_query }}</span>
        </div>
        <div class="tb-note">
          改写只作用于关键词路：语义路靠模型自己跨用词差异，拼同义词反而会稀释语义中心。
        </div>
      </div>

      <!-- ③ 双路召回 -->
      <div v-if="result" class="trace-block">
        <div class="tb-head">
          <span class="tb-num">{{ blockNum }}</span>双路召回
          <span class="tb-count">
            向量 {{ result.retrieval.vec_count }} 条 · 关键词 {{ result.retrieval.bm25_count }} 条
          </span>
        </div>
        <div v-if="!result.references.length" class="tb-muted tb-empty">两路均未召回</div>
        <div v-else class="recall-list">
          <div
            v-for="r in result.references" :key="r.chunk_id"
            class="recall-item" :class="{ 'is-uncited': showCited && !r.cited }"
            @click="$emit('open', r)"
          >
            <span class="ri-rank">#{{ r.index }}</span>
            <span class="ri-doc" :title="r.file_name">{{ r.file_name }}</span>
            <span class="ri-paths">
              <span v-if="r.vec_rank != null" class="path-badge path-vec">向#{{ r.vec_rank }}</span>
              <span v-if="r.bm25_rank != null" class="path-badge path-bm25">词#{{ r.bm25_rank }}</span>
              <span v-if="r.vec_rank == null && r.bm25_rank == null" class="tb-muted">—</span>
            </span>
            <span class="ri-score">{{ fmtScore(r.score) }}</span>
          </div>
        </div>
      </div>

      <!-- ④ 本次统计 -->
      <div v-if="result" class="trace-block">
        <div class="tb-head"><span class="tb-num">{{ statNum }}</span>本次统计</div>
        <div class="tb-row"><span class="tb-key">融合策略</span><span class="tb-val">{{ result.retrieval.fusion_mode || '—' }}</span></div>
        <div class="tb-row"><span class="tb-key">向量 top1</span><span class="tb-val mono">{{ result.retrieval.vec_top1 }}</span></div>
        <div class="tb-row"><span class="tb-key">总耗时</span><span class="tb-val mono">{{ result.latency_ms }} ms</span></div>
        <div v-if="result.llm" class="tb-row">
          <span class="tb-key">LLM</span>
          <span class="tb-val">
            <template v-if="result.llm.ok">
              {{ result.llm.model }} · {{ result.llm.latency_ms }} ms
            </template>
            <template v-else>{{ result.llm.error || '未调用' }}</template>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  result: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  stage: { type: String, default: '' },
  /** 降级场景下 cited 全为 false 属「无判定者」，不应当作「不相关」淡化 */
  showCited: { type: Boolean, default: true },
})
defineEmits(['open'])

const coref = computed(() => props.result?.coreference)
const rewrite = computed(() => props.result?.rewrite)

/** 序号随「指代消解」段是否存在而顺延，避免出现两个① */
const blockNum = computed(() => {
  let n = 1
  if (coref.value?.changed) n++
  if (rewrite.value) n++
  return String(n).replace(/[0-9]/, (d) => '①②③④⑤'[d - 1])
})
const statNum = computed(() => {
  let n = 1
  if (coref.value?.changed) n++
  if (rewrite.value) n++
  n++
  return String(n).replace(/[0-9]/, (d) => '①②③④⑤'[d - 1])
})

function fmtScore(s) {
  if (s == null) return ''
  return s < 1 ? s.toFixed(4) : s.toFixed(2)
}
</script>

<style scoped>
.trace-panel {
  height: 100%;
  overflow-y: auto;
  padding: 12px 14px;
  background: #fafbfc;
  border-left: 1px solid #e4e7ed;
}
.trace-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 12px;
}
.stage-tag {
  margin-left: auto;
  transform: scale(0.86);
}
.trace-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.trace-block {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 10px 12px;
}
.tb-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}
.tb-num {
  color: #409eff;
}
.tb-count {
  margin-left: auto;
  font-size: 11px;
  font-weight: 400;
  color: #909399;
}
.tb-row {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.7;
  margin-bottom: 3px;
}
.tb-key {
  flex-shrink: 0;
  width: 62px;
  color: #909399;
}
.tb-val {
  flex: 1;
  min-width: 0;
  color: #303133;
  word-break: break-all;
}
.tb-val.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px;
  color: #606266;
}
.tb-muted {
  color: #c0c4cc;
}
.tb-empty {
  font-size: 12px;
  text-align: center;
  padding: 6px 0;
}
.tb-note {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed #ebeef5;
  font-size: 11px;
  color: #909399;
  line-height: 1.6;
}
.tb-method {
  margin-top: 6px;
  transform: scale(0.86);
  transform-origin: left center;
}
.kw-tag {
  margin: 0 4px 4px 0;
  transform: scale(0.88);
  transform-origin: left center;
}

/* 双路召回列表 */
.recall-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.recall-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.15s;
}
.recall-item:hover {
  background: #ecf5ff;
}
.recall-item.is-uncited {
  opacity: 0.6;
}
.ri-rank {
  font-weight: 600;
  color: #409eff;
  flex-shrink: 0;
}
.ri-doc {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
}
.ri-paths {
  display: flex;
  gap: 3px;
  flex-shrink: 0;
}
.path-badge {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 3px;
  line-height: 16px;
  white-space: nowrap;
}
.path-vec {
  background: #ecf5ff;
  color: #409eff;
}
.path-bm25 {
  background: #fdf6ec;
  color: #e6a23c;
}
.ri-score {
  font-size: 10px;
  color: #c0c4cc;
  font-family: ui-monospace, Consolas, monospace;
  flex-shrink: 0;
  width: 44px;
  text-align: right;
}
</style>
