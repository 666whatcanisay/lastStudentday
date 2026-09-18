<template>
  <!--
    智能问答页签 —— 人机交互的主体。

    设计要点（详见 UI_INTERACTION_PLAN.md）：
      · 左侧对话区：问题 / 回答 / 引用卡片
      · 右侧 320px 常驻「检索过程」侧栏：把检索链路摊开给用户看
      · 回答里的 [n] 是可点的 —— 点开原文并高亮命中区间
      · 降级不藏：检索为空 / LLM 未配置 / 向量模型不可用，都明确告知

    为什么侧栏常驻而不折叠：这是教学演示系统，双路检索的互补性、查询改写的
    作用、RRF 融合的效果，正是最值得展示的东西。折叠起来就成了藏在细节里的卖点。
  -->
  <div class="qa-wrap">
    <!-- ============ 左：对话区 ============ -->
    <div class="qa-main">
      <!-- ---- 状态条：索引 / LLM 可用性 ---- -->
      <div v-if="banner.show" class="qa-banner">
        <el-alert
          :type="banner.type"
          :closable="false"
          show-icon
          class="banner-alert"
        >
          <template #title>
            <span class="banner-title">{{ banner.title }}</span>
            <el-button
              v-if="banner.action"
              size="small"
              type="primary"
              link
              :loading="indexing"
              @click="doRebuildIndex"
            >
              {{ banner.actionText }}
            </el-button>
          </template>
          <div class="banner-desc">{{ banner.desc }}</div>
        </el-alert>
      </div>

      <!-- ---- 设置区（可折叠） ---- -->
      <div class="qa-settings">
        <div class="qs-bar" @click="showSettings = !showSettings">
          <el-icon><Setting /></el-icon>
          <span>检索设置</span>
          <el-tag size="small" type="info" effect="plain" class="qs-summary">{{ settingSummary }}</el-tag>
          <el-icon class="qs-arrow" :class="{ 'is-open': showSettings }"><ArrowDown /></el-icon>
        </div>
        <el-collapse-transition>
          <div v-show="showSettings" class="qs-body">
            <div class="qs-item">
              <label>检索模式</label>
              <el-radio-group v-model="mode" size="small">
                <el-radio-button value="hybrid">双路融合</el-radio-button>
                <el-radio-button value="vec">仅向量</el-radio-button>
                <el-radio-button value="bm25">仅关键词</el-radio-button>
              </el-radio-group>
              <span class="qs-hint">{{ MODE_HINT[mode] }}</span>
            </div>
            <div class="qs-item">
              <label>查询改写</label>
              <el-switch v-model="rewrite" size="small" />
              <span class="qs-hint">相对时间解析 + 同义扩展，只作用于关键词路</span>
            </div>
            <div class="qs-item">
              <label>多轮上下文</label>
              <el-switch v-model="multiTurn" size="small" />
              <span class="qs-hint">开启后可追问「它的税额是多少」，系统自动消解指代</span>
            </div>
            <div class="qs-item">
              <label>返回条数</label>
              <el-input-number v-model="topK" :min="1" :max="20" size="small" style="width: 110px" />
              <span class="qs-hint">送入 Prompt 的资料条数</span>
            </div>
            <div class="qs-item">
              <label>限定文档</label>
              <el-select
                v-model="docIds"
                multiple
                collapse-tags
                collapse-tags-tooltip
                clearable
                filterable
                placeholder="不限（检索全部已索引文档）"
                size="small"
                style="width: 320px"
              >
                <el-option
                  v-for="d in docOptions"
                  :key="d.id"
                  :label="d.file_name"
                  :value="d.id"
                />
              </el-select>
            </div>
          </div>
        </el-collapse-transition>
      </div>

      <!-- ---- 消息流 ---- -->
      <div ref="chatEl" class="qa-chat">
        <!-- 空态：给示例问题，降低上手成本 -->
        <div v-if="messages.length === 0" class="qa-empty">
          <el-icon class="qe-icon"><ChatDotRound /></el-icon>
          <div class="qe-title">向你的文档提问</div>
          <div class="qe-desc">
            系统会先在本地做双路检索（向量语义 + BM25 关键词），再把命中的原文片段交给大模型作答，
            每条结论都能点回原文核对。
          </div>
          <div class="qe-samples">
            <span class="qe-samples-label">试试：</span>
            <el-tag
              v-for="s in SAMPLES"
              :key="s"
              class="qe-sample"
              effect="plain"
              @click="input = s; send()"
            >{{ s }}</el-tag>
          </div>
        </div>

        <div v-for="m in messages" :key="m.id" class="msg" :class="'msg-' + m.role">
          <!-- 用户 -->
          <template v-if="m.role === 'user'">
            <div class="msg-bubble user-bubble">{{ m.text }}</div>
            <div class="msg-avatar user-avatar"><el-icon><User /></el-icon></div>
          </template>

          <!-- 助手 -->
          <template v-else>
            <div class="msg-avatar bot-avatar"><el-icon><MagicStick /></el-icon></div>
            <div class="msg-bubble bot-bubble">
              <!-- 加载中 -->
              <div v-if="m.loading" class="ans-loading">
                <el-icon class="is-loading"><Loading /></el-icon>
                <span>{{ stage || '处理中…' }}</span>
              </div>

              <!-- 请求异常 -->
              <el-alert v-else-if="m.error" type="error" :closable="false" show-icon
                        :title="'请求失败：' + m.error" />

              <template v-else-if="m.result">
                <!-- 降级提示：明确告诉用户发生了什么，不装作正常 -->
                <el-alert
                  v-if="m.result.degrade_reason === 'no_hits'"
                  type="warning" :closable="false" show-icon
                  title="未检索到相关资料"
                  class="ans-alert"
                >
                  <div>换一种问法，或先把文档建立索引。也可以点右侧「检索过程」看两路各自的召回情况。</div>
                </el-alert>
                <el-alert
                  v-else-if="m.result.degrade_reason === 'llm_not_configured'"
                  type="info" :closable="false" show-icon
                  title="未配置大模型，已降级为「仅检索」"
                  class="ans-alert"
                >
                  <div>把 DeepSeek Key 填进 <code>doc-ocr-system\.env</code> 的 <code>LLM_API_KEY</code> 后重启即可生成回答。以下是检索到的原文片段。</div>
                </el-alert>
                <el-alert
                  v-else-if="m.result.degrade_reason === 'llm_call_failed'"
                  type="error" :closable="false" show-icon
                  title="大模型调用失败，已降级为「仅检索」"
                  class="ans-alert"
                >
                  <div>{{ m.result.llm?.error || '未知错误' }}。以下是检索到的原文片段。</div>
                </el-alert>
                <el-alert
                  v-if="m.result.retrieval?.degraded && m.result.retrieval?.degrade_reason === 'vector_model_unavailable'"
                  type="warning" :closable="false" show-icon
                  title="向量模型不可用，本次已自动降级为关键词检索"
                  class="ans-alert"
                />

                <!-- 回答正文：[n] 渲染成可点角标。
                     角标带方括号而不是裸数字 —— 裸上标复制出来是「55,000.00元 1。」，
                     看不出那个 1 是引用标记。 -->
                <div v-if="m.result.answer" class="ans-text">
                  <template v-for="(p, i) in parseAnswer(m.result.answer)" :key="i">
                    <span v-if="p.type === 'text'">{{ p.text }}</span>
                    <sup
                      v-else
                      class="cite-chip"
                      :class="{ 'is-uncited': !isCited(m.result, p.n) }"
                      :title="citeTitle(m.result, p.n)"
                      @click="openRef(m, p.n)"
                    >[{{ p.n }}]</sup>
                  </template>
                </div>
                <div v-else class="ans-text ans-fallback">
                  本次未生成回答（降级为仅检索模式，见上方说明）。
                </div>

                <!-- 未引用提示：回答没被任何检索片段支撑时说清楚 -->
                <div v-if="m.result.answer && refsAllUncited(m.result)" class="ans-unsupported">
                  <el-icon><WarningFilled /></el-icon>
                  <span>模型未引用任何检索片段，这段回答未由你的资料支撑，请谨慎采用。</span>
                </div>

                <!-- 引用区 -->
                <div v-if="m.result.references && m.result.references.length" class="ans-refs">
                  <div class="ar-head">
                    <el-icon><Link /></el-icon>
                    <!-- 0 条被引用时它们就不是"引用来源"了，措辞必须跟着变，
                         否则「引用来源」+「被引用 0/4」自相矛盾 -->
                    <span>{{ m.result.degraded || refsAllUncited(m.result)
                      ? '检索到的原文片段' : '引用来源' }}</span>
                    <el-tag
                      v-if="!m.result.degraded"
                      size="small"
                      :type="citedCount(m.result) ? 'success' : 'info'"
                      effect="plain"
                    >
                      {{ citedCount(m.result)
                        ? `被引用 ${citedCount(m.result)} / ${m.result.references.length}`
                        : '模型未引用' }}
                    </el-tag>
                    <span class="ar-hint">点击卡片查看原文并高亮命中位置</span>
                  </div>
                  <div class="ar-list">
                    <CitationCard
                      v-for="r in m.result.references"
                      :key="r.chunk_id"
                      :ref-item="r"
                      :show-cited="!m.result.degraded"
                      :active="drawerRef && drawerRef.chunk_id === r.chunk_id"
                      @open="openDrawer(m, r)"
                    />
                  </div>
                </div>

                <!-- 耗时 / 模型信息 -->
                <div class="ans-foot">
                  <span>总耗时 {{ m.result.latency_ms }} ms</span>
                  <span v-if="m.result.llm?.ok">· {{ m.result.llm.model }} {{ m.result.llm.latency_ms }} ms</span>
                  <span v-if="m.result.retrieval">· 召回 向量{{ m.result.retrieval.vec_count }} / 关键词{{ m.result.retrieval.bm25_count }}</span>
                </div>
              </template>
            </div>
          </template>
        </div>
      </div>

      <!-- ---- 输入区 ---- -->
      <div class="qa-input">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          :disabled="sending"
          :placeholder="inputPlaceholder"
          @keydown.enter.exact.prevent="send"
        />
        <div class="qi-right">
          <el-button
            v-if="messages.length"
            size="small"
            text
            :disabled="sending"
            @click="clearChat"
          >
            <el-icon><Delete /></el-icon> 清空
          </el-button>
          <el-button type="primary" :loading="sending" @click="send">
            <el-icon v-if="!sending"><Promotion /></el-icon>
            {{ sending ? '处理中' : '提问' }}
          </el-button>
        </div>
      </div>
      <div class="qa-tip">
        Enter 发送 · Shift+Enter 换行 · 阶段提示为前端推测，非服务端实时进度
      </div>
    </div>

    <!-- ============ 右：检索过程侧栏 ============ -->
    <aside class="qa-trace">
      <RetrievalTrace
        :result="currentResult"
        :loading="sending"
        :stage="stage"
        :show-cited="showCited"
        @open="onTraceOpen"
      />
    </aside>

    <!-- ============ 原文高亮抽屉 ============ -->
    <SourceDrawer
      v-model="drawerVisible"
      :ref-item="drawerRef"
      :show-cited="drawerShowCited"
      :has-prev="drawerIdx > 0"
      :has-next="drawerIdx >= 0 && drawerIdx < drawerList.length - 1"
      @prev="stepRef(-1)"
      @next="stepRef(1)"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import {
  ragAsk,
  getRagConfig,
  getRagIndexStatus,
  rebuildRagIndex,
  getDocumentList,
} from '../api/index.js'
import CitationCard from './CitationCard.vue'
import RetrievalTrace from './RetrievalTrace.vue'
import SourceDrawer from './SourceDrawer.vue'

// ==================== 常量 ====================
const MODE_HINT = {
  hybrid: '向量做语义召回，关键词做相关性判定，RRF 融合 —— 推荐',
  vec: '只看语义相似度。短查询区分力弱，仅作诊断视图',
  bm25: '只看关键词。语义不同的同义表述会漏召，仅作诊断视图',
}
const SAMPLES = [
  '这张发票的价税合计是多少',
  '有哪些客运服务费发票',
  '最近一个月的打车花了多少钱',
]

// ==================== 状态 ====================
const showSettings = ref(false)
const mode = ref('hybrid')
const rewrite = ref(true)
const multiTurn = ref(true)
const topK = ref(5)
const docIds = ref([])
const docOptions = ref([])

const input = ref('')
const sending = ref(false)
const stage = ref('')
const messages = ref([])
const turns = ref([])          // 多轮历史（前端持有，后端无状态）
const currentResult = ref(null)
let seq = 0

const chatEl = ref(null)
const indexing = ref(false)
const ragConfig = ref(null)
const indexStatus = ref(null)

// 抽屉
const drawerVisible = ref(false)
const drawerRef = ref(null)
const drawerList = ref([])
const drawerIdx = ref(-1)
/** 当前浏览的这批引用是否属于降级回答（降级时不该标「未被引用」） */
const drawerShowCited = ref(true)

// ==================== 阶段推测提示 ====================
// 后端未做 SSE，这里按经验时长推进阶段文案。
// 刻意标注「推测」—— 不能让用户以为是真实进度。
const STAGES = ['理解问题', '改写查询', '双路召回', '融合排序', '生成回答']
let stageTimer = null
function startStage() {
  let i = 0
  stage.value = STAGES[0] + '…'
  stageTimer = setInterval(() => {
    if (i < STAGES.length - 1) {
      i += 1
      stage.value = STAGES[i] + '…'
    }
  }, 900)
}
function stopStage() {
  if (stageTimer) clearInterval(stageTimer)
  stageTimer = null
  stage.value = ''
}

// ==================== 顶部状态条 ====================
const banner = computed(() => {
  const s = indexStatus.value
  const c = ragConfig.value
  if (!c) return { show: false }

  if (c.enabled === false) {
    return {
      show: true, type: 'info', title: 'RAG 功能未启用',
      desc: '在 .env 中设置 RAG_ENABLED=True 后重启服务。',
    }
  }
  if (s && s.total_docs === 0) {
    return {
      show: true, type: 'info', title: '还没有可用于检索的文档',
      desc: '先到「文档管理」上传并识别文档，然后建立索引。',
    }
  }
  if (s && s.never_indexed && s.never_indexed.length > 0) {
    return {
      show: true, type: 'warning',
      title: `有 ${s.never_indexed.length} 篇文档尚未建立索引`,
      desc: `已索引 ${s.indexed_docs} / ${s.total_docs} 篇。未索引的文档无法被检索到。`,
      action: 'index', actionText: '立即建立索引',
    }
  }
  if (c.llm && c.llm.configured === false) {
    return {
      show: true, type: 'warning', title: '未配置大模型，问答将降级为「仅检索」',
      desc: c.llm.degrade_hint || '把 API Key 填进 .env 的 LLM_API_KEY 后重启服务。',
    }
  }
  if (c.embedding && c.embedding.ready === false) {
    return {
      show: true, type: 'warning', title: '向量模型未就绪，将自动降级为关键词检索',
      desc: c.embedding.error || '检查模型文件是否完整。',
    }
  }
  return { show: false }
})

const settingSummary = computed(() => {
  const m = { hybrid: '双路融合', vec: '仅向量', bm25: '仅关键词' }[mode.value] || mode.value
  const parts = [m]
  if (rewrite.value) parts.push('改写')
  if (multiTurn.value) parts.push('多轮')
  if (docIds.value.length) parts.push(`限定${docIds.value.length}篇`)
  parts.push(`Top${topK.value}`)
  return parts.join(' · ')
})

const inputPlaceholder = computed(() =>
  multiTurn.value && turns.value.length
    ? '继续追问，例如「它的税额是多少」…（Enter 发送）'
    : '输入你的问题…（Enter 发送，Shift+Enter 换行）'
)

/**
 * 降级时不展示「被引用 / 未被引用」的区分。
 * 降级意味着没有模型做过引用判定，cited 全 false 是「无判定者」而非「判定为不相关」，
 * 继续淡化会让用户误以为这些片段不相关 —— 恰恰相反，它们是本次唯一可看的内容。
 */
const showCited = computed(() => !currentResult.value?.degraded)

// ==================== 多轮历史组装 ====================
// 只取最近 6 轮：再长对指代消解无益，反而增加 LLM 成本
const historyPayload = computed(() => {
  if (!multiTurn.value || turns.value.length === 0) return null
  return turns.value.slice(-6).map((t) => ({
    question: t.question,
    answer: t.answer,
    top_doc_id: t.topRef?.doc_id ?? null,
    top_file_name: t.topRef?.file_name ?? null,
    top_chunk_text: t.topRef?.chunk_text ?? null,
  }))
})

// ==================== 回答正文解析 ====================
/** 把回答里的 [n] 切成可交互角标。不用 v-html —— 回答来自模型，不做 HTML 注入 */
function parseAnswer(text) {
  if (!text) return []
  const parts = []
  const re = /\[(\d+)\]/g
  let last = 0
  let m
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push({ type: 'text', text: text.slice(last, m.index) })
    parts.push({ type: 'cite', n: Number(m[1]) })
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push({ type: 'text', text: text.slice(last) })
  return parts
}

function findRef(result, n) {
  return (result?.references || []).find((r) => r.index === n)
}
function isCited(result, n) {
  return !!findRef(result, n)?.cited
}
function citeTitle(result, n) {
  const r = findRef(result, n)
  if (!r) return `引用 [${n}] 不存在`
  return `${r.file_name}${r.page_no ? ' 第' + r.page_no + '页' : ''}${r.cited ? '' : '（未被模型引用）'}`
}
function citedCount(result) {
  return (result?.references || []).filter((r) => r.cited).length
}
/** 模型一条片段都没引用。
 *
 *  依据是 cited 数据本身，**不去猜回答文本**（"无法回答""没有相关信息"这类
 *  字符串匹配太脆，模型换个说法就失效）。0 条被引用有两种可能：
 *  ①模型判断资料不相关而拒答 ②模型自己会答但没用上资料。
 *  两种情况对用户的含义都是"这段回答没被资料支撑"，所以合并提示即可。 */
function refsAllUncited(result) {
  const refs = result?.references || []
  return !result?.degraded && refs.length > 0 && citedCount(result) === 0
}

// ==================== 发送 ====================
async function send() {
  const q = input.value.trim()
  if (!q || sending.value) return

  messages.value.push(reactive({ id: ++seq, role: 'user', text: q }))
  const am = reactive({ id: ++seq, role: 'assistant', loading: true, result: null, error: '' })
  messages.value.push(am)

  input.value = ''
  sending.value = true
  startStage()
  scrollToBottom()

  try {
    const res = await ragAsk({
      question: q,
      mode: mode.value,
      topK: topK.value,
      docIds: docIds.value.length ? docIds.value : null,
      rewrite: rewrite.value,
      history: historyPayload.value,
    })
    am.result = res.data
    currentResult.value = res.data
    // Top-1 命中作为下一轮指代消解的锚点来源
    turns.value.push({
      question: q,
      answer: res.data.answer || null,
      topRef: res.data.references?.[0] || null,
    })
  } catch (e) {
    am.error = e?.message || '未知错误'
  } finally {
    am.loading = false
    sending.value = false
    stopStage()
    scrollToBottom()
  }
}

function clearChat() {
  messages.value = []
  turns.value = []
  currentResult.value = null
}

function scrollToBottom() {
  nextTick(() => {
    if (chatEl.value) chatEl.value.scrollTop = chatEl.value.scrollHeight
  })
}

// ==================== 原文抽屉 ====================
/** 从回答里的 [n] 角标打开 */
function openRef(msg, n) {
  const r = findRef(msg.result, n)
  if (!r) return
  openDrawer(msg, r)
}
/** 从侧栏召回列表打开 */
function onTraceOpen(r) {
  const msg = [...messages.value].reverse().find((m) => m.result?.references?.includes(r))
  openDrawer(msg, r)
}
function openDrawer(msg, refItem) {
  const list = msg?.result?.references || []
  drawerList.value = list
  drawerIdx.value = list.findIndex((x) => x.chunk_id === refItem.chunk_id)
  drawerRef.value = refItem
  drawerShowCited.value = !msg?.result?.degraded
  drawerVisible.value = true
}
function stepRef(delta) {
  const next = drawerIdx.value + delta
  if (next < 0 || next >= drawerList.value.length) return
  drawerIdx.value = next
  drawerRef.value = drawerList.value[next]
}

// ==================== 索引与配置 ====================
async function loadConfig() {
  try {
    const res = await getRagConfig()
    ragConfig.value = res.data
  } catch (e) { /* 拦截器已提示 */ }
}
async function loadIndexStatus() {
  try {
    const res = await getRagIndexStatus()
    indexStatus.value = res.data
  } catch (e) { /* ignore */ }
}
async function doRebuildIndex() {
  indexing.value = true
  try {
    const res = await rebuildRagIndex(null)
    ElMessage.success(res.msg || '索引重建完成')
    await loadIndexStatus()
  } catch (e) { /* 拦截器已提示 */ } finally {
    indexing.value = false
  }
}
async function loadDocOptions() {
  try {
    const res = await getDocumentList({ page: 1, page_size: 100 })
    docOptions.value = (res.data.items || []).filter((d) => d.status === 1)
  } catch (e) { /* ignore */ }
}

onMounted(() => {
  loadConfig()
  loadIndexStatus()
  loadDocOptions()
})

defineExpose({ refresh: () => { loadConfig(); loadIndexStatus(); loadDocOptions() } })
</script>

<style scoped>
/* ============ 整体两栏 ============
   高度由外层弹性布局传递（App.vue 里 .pane-fit 是 height:100%），
   这里不再写 calc(100vh - Npx) 之类的魔数 —— 那样任何一处边距变动都会溢出视口。 */
.qa-wrap {
  display: flex;
  height: 100%;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: hidden;
}
.qa-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.qa-trace {
  width: 320px;
  flex-shrink: 0;
  min-height: 0;
}

/* ============ 状态条 ============ */
.qa-banner { padding: 10px 12px 0; flex-shrink: 0; }
.banner-alert :deep(.el-alert__content) { width: 100%; }
.banner-title { font-weight: 600; }
.banner-desc { font-size: 12px; margin-top: 2px; line-height: 1.6; }

/* ============ 设置区 ============ */
.qa-settings {
  flex-shrink: 0;
  border-bottom: 1px solid #ebeef5;
}
.qs-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  user-select: none;
}
.qs-bar:hover { background: #fafbfc; }
.qs-summary { margin-left: 4px; transform: scale(0.88); }
.qs-arrow {
  margin-left: auto;
  color: #909399;
  transition: transform 0.2s;
}
.qs-arrow.is-open { transform: rotate(180deg); }
.qs-body {
  padding: 4px 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.qs-item {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.qs-item > label {
  width: 72px;
  flex-shrink: 0;
  font-size: 12px;
  color: #606266;
}
.qs-hint {
  font-size: 11px;
  color: #909399;
}

/* ============ 消息流 ============ */
.qa-chat {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: #fafbfc;
  min-height: 0;
}
.qa-empty {
  max-width: 520px;
  margin: 8vh auto 0;
  text-align: center;
  color: #909399;
}
.qe-icon { font-size: 42px; color: #c0c4cc; }
.qe-title { font-size: 16px; font-weight: 600; color: #303133; margin: 10px 0 6px; }
.qe-desc { font-size: 13px; line-height: 1.8; }
.qe-samples {
  margin-top: 18px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  align-items: center;
}
.qe-samples-label { font-size: 12px; }
.qe-sample { cursor: pointer; }
.qe-sample:hover { background: #ecf5ff; border-color: #409eff; }

.msg {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
  align-items: flex-start;
}
.msg-user { justify-content: flex-end; }
.msg-avatar {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #fff;
  font-size: 16px;
}
.user-avatar { background: linear-gradient(135deg, #67C23A, #85ce61); order: 2; }
.bot-avatar { background: linear-gradient(135deg, #409EFF, #36cfc9); }

.msg-bubble {
  max-width: calc(100% - 90px);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.75;
}
.user-bubble {
  background: #409eff;
  color: #fff;
  white-space: pre-wrap;
  word-break: break-word;
}
.bot-bubble {
  background: #fff;
  border: 1px solid #e4e7ed;
  color: #303133;
  flex: 1;
  min-width: 0;
}
.ans-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  font-size: 13px;
}
.ans-alert { margin-bottom: 10px; }
.ans-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.ans-fallback { color: #909399; }

/* 「回答未被资料支撑」提示。
   刻意做得比 el-alert 轻 —— 它不是错误，只是一条需要用户知情的状态，
   用重色会让人误以为系统坏了。 */
.ans-unsupported {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  background: #fdf6ec;
  border-left: 3px solid #e6a23c;
  color: #b88230;
  font-size: 12px;
  line-height: 1.6;
}
.ans-unsupported .el-icon {
  margin-top: 2px;
  flex-shrink: 0;
}
/* 引用角标：被引用的实心，未被引用的淡化 —— 与引用卡片视觉规则一致 */
.cite-chip {
  display: inline-block;
  min-width: 16px;
  height: 16px;
  line-height: 16px;
  text-align: center;
  padding: 0 3px;
  margin: 0 1px;
  border-radius: 3px;
  background: #409eff;
  color: #fff;
  font-size: 11px;
  cursor: pointer;
  vertical-align: super;
  transition: all 0.15s;
}
.cite-chip:hover { background: #66b1ff; }
.cite-chip.is-uncited {
  background: #e4e7ed;
  color: #909399;
}
.cite-chip.is-uncited:hover { background: #c0c4cc; color: #606266; }

.ans-refs { margin-top: 14px; }
.ar-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 8px;
}
.ar-hint {
  font-weight: 400;
  color: #c0c4cc;
  font-size: 11px;
}
.ar-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ans-foot {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #ebeef5;
  font-size: 11px;
  color: #c0c4cc;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

/* ============ 输入区 ============ */
.qa-input {
  flex-shrink: 0;
  border-top: 1px solid #ebeef5;
  padding: 10px 14px 6px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
  background: #fff;
}
.qa-input :deep(.el-textarea) { flex: 1; }
.qi-right {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 2px;
}
.qa-tip {
  flex-shrink: 0;
  padding: 0 14px 8px;
  font-size: 11px;
  color: #c0c4cc;
  background: #fff;
}

/* ============ 窄屏适配 ============
   这是桌面 B/S 系统，窄屏只做降级处理，不做完整移动端适配。
   刻意**不隐藏**「检索过程」侧栏 —— 它是这套系统的核心卖点，
   窄屏下宁可改成上下堆叠，也不该把可解释性藏起来。
   （原计划写的是「折叠为顶部抽屉」，实际做下来堆叠更简单且信息不丢，
     这里按实际实现修正。） */
@media (max-width: 1199px) {
  .qa-trace { width: 264px; }
}
@media (max-width: 767px) {
  .qa-wrap { flex-direction: column; }
  .qa-main { min-height: 0; }
  .qa-trace {
    width: 100%;
    height: 240px;
    flex-shrink: 0;
  }
  .trace-panel { border-left: none; border-top: 1px solid #e4e7ed; }
  .qs-item > label { width: 100%; }
  .msg-bubble { max-width: calc(100% - 50px); }
}
</style>
