<template>
  <el-dialog
    v-model="open"
    :title="topic.title"
    :width="mobile ? '100%' : 'min(980px, calc(100vw - 40px))'"
    :fullscreen="mobile"
    class="project-tech-dialog"
    :close-on-click-modal="false"
    destroy-on-close
    @closed="emit('close')"
  >
    <div ref="contentRef" class="tech-dialog-content">
      <div class="dialog-intro">
        <span class="dialog-kicker">TECHNICAL WALKTHROUGH · {{ topic.number }} / 06</span>
        <h3>30 秒讲法</h3>
        <p>{{ topic.talk }}</p>
        <div class="tech-tags" aria-label="主要技术">
          <span v-for="name in topic.techniques" :key="name">{{ name }}</span>
        </div>
      </div>

      <section class="dialog-section" aria-labelledby="topic-workflow-title">
        <div class="dialog-section-heading">
          <span class="dialog-kicker">PROCESS</span>
          <h3 id="topic-workflow-title">这一模块怎样运转</h3>
        </div>
        <ol class="topic-steps">
          <li v-for="(step, index) in topic.steps" :key="step.title">
            <span class="topic-step-number">{{ String(index + 1).padStart(2, '0') }}</span>
            <div><strong>{{ step.title }}</strong><p>{{ step.detail }}</p></div>
          </li>
        </ol>
      </section>

      <section class="dialog-section" aria-labelledby="topic-trace-title">
        <div class="dialog-section-heading">
          <span class="dialog-kicker">SOURCE MAP</span>
          <h3 id="topic-trace-title">操作、接口与函数怎么对应</h3>
          <p>以下路径相对项目根目录；函数名对应当前代码，不依赖固定行号。</p>
        </div>
        <div class="trace-table" role="table" aria-label="模块代码调用链">
          <div class="trace-head" role="row">
            <span role="columnheader">用户操作 / 阶段</span>
            <span role="columnheader">接口</span>
            <span role="columnheader">代码定位</span>
          </div>
          <div v-for="row in topic.trace" :key="row.action" class="trace-row" role="row">
            <div role="cell"><span class="mobile-cell-label">操作 / 阶段</span><strong>{{ row.action }}</strong></div>
            <div role="cell"><span class="mobile-cell-label">接口</span><code>{{ row.api }}</code></div>
            <div role="cell" class="trace-code">
              <span class="mobile-cell-label">代码定位</span>
              <code v-for="path in row.code" :key="path">{{ path }}</code>
            </div>
          </div>
        </div>
      </section>

      <section class="dialog-section boundary-section" aria-labelledby="topic-boundary-title">
        <div class="dialog-section-heading">
          <span class="dialog-kicker">BOUNDARIES</span>
          <h3 id="topic-boundary-title">边界与失败时的表现</h3>
        </div>
        <ul><li v-for="item in topic.boundaries" :key="item">{{ item }}</li></ul>
      </section>

      <div class="dialog-actions">
        <div class="topic-paging">
          <el-button :disabled="topicIndex === 0" @click="changeTopic(-1)">上一专题</el-button>
          <el-button :disabled="topicIndex === topics.length - 1" @click="changeTopic(1)">下一专题</el-button>
        </div>
        <el-button v-if="topic.tab !== 'data' || isAdmin" type="primary" @click="navigateToFeature">
          进入{{ topic.tab === 'docs' ? '文档管理' : topic.tab === 'analytics' ? '统计' : topic.tab === 'qa' ? '智能问答' : '数据管理' }}
        </el-button>
        <span v-else class="restricted-note">数据管理仅管理员可进入</span>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { topics } from './projectIntroContent.js'

const props = defineProps({
  topic: { type: Object, required: true },
  isAdmin: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'navigate', 'select'])
const open = ref(true)
const mobile = ref(window.innerWidth <= 650)
const contentRef = ref(null)
const topicIndex = computed(() => topics.findIndex(item => item.id === props.topic.id))

function updateViewport() { mobile.value = window.innerWidth <= 650 }
function changeTopic(delta) {
  const next = topics[topicIndex.value + delta]
  if (next) emit('select', next.id)
}
function navigateToFeature() {
  if (props.topic.tab === 'data' && !props.isAdmin) return
  open.value = false
  emit('navigate', props.topic.tab)
}

watch(() => props.topic.id, async () => {
  await nextTick()
  contentRef.value?.parentElement?.scrollTo({ top: 0 })
})
onMounted(() => window.addEventListener('resize', updateViewport))
onUnmounted(() => window.removeEventListener('resize', updateViewport))
</script>

<style>
.project-tech-dialog { max-width: 980px; margin-top: 5vh !important; border-radius: 13px; overflow: hidden; }
.project-tech-dialog .el-dialog__header { margin-right: 0; padding: 20px 56px 17px 28px; border-bottom: 1px solid #e0eae7; }
.project-tech-dialog .el-dialog__title { color: #193d45; font-size: 21px; font-weight: 700; }
.project-tech-dialog .el-dialog__body { padding: 0; max-height: calc(100vh - 130px); overflow-y: auto; }
.tech-dialog-content { padding: 24px 28px 28px; color: #193d45; }
.dialog-kicker { color: #367886; font-size: 11px; font-weight: 700; letter-spacing: .1em; }
.dialog-intro { padding: 20px 22px; border-radius: 10px; background: #eaf4f2; border: 1px solid #d1e6e2; }
.dialog-intro h3, .dialog-section-heading h3 { margin: 6px 0 8px; font-size: 19px; }
.dialog-intro p, .dialog-section-heading p { margin: 0; color: #4d686f; line-height: 1.72; }
.dialog-intro p { font-size: 15px; }
.tech-tags { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 15px; }
.tech-tags span { padding: 4px 9px; border: 1px solid #c7dfda; border-radius: 5px; background: #fff; color: #2a6771; font-size: 12px; }
.dialog-section { margin-top: 24px; }
.dialog-section-heading { margin-bottom: 14px; }
.dialog-section-heading p { font-size: 12px; }
.topic-steps { list-style: none; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 0; padding: 0; }
.topic-steps li { min-width: 0; padding: 15px; border: 1px solid #dfebe8; border-radius: 8px; background: #f7faf9; }
.topic-step-number { display: block; color: #39808a; font-weight: 700; font-size: 12px; margin-bottom: 10px; }
.topic-steps strong { font-size: 14px; }
.topic-steps p { margin: 7px 0 0; color: #587078; font-size: 12px; line-height: 1.65; }
.trace-table { border: 1px solid #dce8e5; border-radius: 8px; overflow: hidden; }
.trace-head, .trace-row { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr) minmax(0, 1.8fr); }
.trace-head { background: #edf5f3; color: #386570; font-size: 12px; font-weight: 700; }
.trace-head > span, .trace-row > div { min-width: 0; padding: 12px 13px; }
.trace-row { border-top: 1px solid #e5eeec; }
.trace-row > div + div { border-left: 1px solid #e5eeec; }
.trace-row strong { font-size: 13px; }
.trace-row code { display: block; color: #315e67; font-size: 12px; line-height: 1.55; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }
.trace-code code + code { margin-top: 5px; }
.mobile-cell-label { display: none; }
.boundary-section { padding: 18px 20px; border: 1px solid #e8e5da; border-radius: 8px; background: #fcfaf4; }
.boundary-section ul { padding-left: 20px; margin: 0; color: #5d6663; font-size: 13px; line-height: 1.72; }
.boundary-section li + li { margin-top: 7px; }
.dialog-actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding-top: 22px; }
.topic-paging { display: flex; gap: 8px; }
.restricted-note { color: #73878b; font-size: 12px; }
@media (max-width: 650px) {
  .project-tech-dialog.is-fullscreen { margin: 0 !important; max-width: none; border-radius: 0; }
  .project-tech-dialog .el-dialog__header { padding: 17px 50px 14px 18px; }
  .project-tech-dialog .el-dialog__title { font-size: 18px; }
  .project-tech-dialog.is-fullscreen .el-dialog__body { max-height: calc(100vh - 64px); height: calc(100vh - 64px); }
  .tech-dialog-content { padding: 18px 15px 26px; }
  .dialog-intro { padding: 17px; }
  .topic-steps { grid-template-columns: 1fr; }
  .trace-head { display: none; }
  .trace-row { display: block; padding: 8px 12px; }
  .trace-row > div { padding: 5px 0; }
  .trace-row > div + div { border-left: 0; }
  .mobile-cell-label { display: block; color: #789097; font-size: 11px; margin-bottom: 3px; }
  .dialog-actions { align-items: stretch; flex-direction: column; }
  .topic-paging { width: 100%; }
  .topic-paging .el-button { flex: 1; }
}
</style>
