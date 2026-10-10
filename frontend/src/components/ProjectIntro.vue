<template>
  <div class="project-intro">
    <section class="hero" aria-labelledby="intro-title">
      <div class="hero-copy">
        <span class="eyebrow">项目介绍 · 智能文档工作台</span>
        <h1 id="intro-title">把图片和 PDF 变成可校对、可统计、可导出、可查证的资料</h1>
        <p>
          文件存下来只是第一步。这个工作台把本地识别、字段抽取、人工校对与后续使用串成一条流程，
          让信息不必每次都从原件中重新寻找。
        </p>
        <div class="hero-actions">
          <el-button type="primary" @click="go('docs')">从一份文档开始</el-button>
          <el-button plain @click="openTopic('architecture')">看技术实现 · 6 个专题</el-button>
          <span>演示路线：文档管理 → 统计 → 智能问答<span v-if="isAdmin"> → 数据管理</span></span>
        </div>
      </div>
      <div class="hero-mark" aria-hidden="true">
        <div class="paper paper-back"></div>
        <div class="paper paper-front"><span></span><span></span><span></span></div>
        <div class="mark-check">✓</div>
      </div>
    </section>

    <section class="section-card" aria-labelledby="workflow-title">
      <div class="section-heading">
        <span class="section-kicker">WORKFLOW</span>
        <h2 id="workflow-title">一份文档如何变成可用资料</h2>
        <p>OCR 负责读字，人负责确认业务事实；当前有效版本贯穿后续功能。</p>
      </div>
      <ol class="workflow">
        <li v-for="step in steps" :key="step.number" class="workflow-step">
          <span class="step-number">{{ step.number }}</span>
          <strong>{{ step.title }}</strong>
          <span>{{ step.description }}</span>
        </li>
      </ol>
      <div class="outcomes" aria-label="处理结果可以用于">
        <span class="outcome-label">有效版本可用于</span>
        <span>统计与明细</span>
        <span>PDF／Excel 导出</span>
        <span>查证与问答</span>
      </div>
    </section>

    <section class="features" aria-labelledby="features-title">
      <div class="section-heading">
        <span class="section-kicker">TECHNICAL MODULES</span>
        <h2 id="features-title">六条技术链，把功能讲到代码里</h2>
        <p>先看 30 秒讲法，再沿工作流追到接口与函数；也可以直接进入对应板块。</p>
      </div>
      <div class="feature-grid">
        <article v-for="topic in topics" :key="topic.id" class="feature-card">
          <div class="feature-top">
            <div class="feature-icon" aria-hidden="true"><el-icon><component :is="topicIcons[topic.id]" /></el-icon></div>
            <span class="feature-number">{{ topic.number }} / 06</span>
          </div>
          <h3>{{ topic.title }}</h3>
          <p>{{ topic.summary }}</p>
          <div class="feature-tech"><span v-for="name in topic.techniques.slice(0, 2)" :key="name">{{ name }}</span></div>
          <div class="feature-actions">
            <el-button type="primary" @click="openTopic(topic.id)">查看实现</el-button>
            <el-button v-if="topic.tab !== 'data' || isAdmin" text @click="go(topic.tab)">
              进入{{ topic.tab === 'docs' ? '文档管理' : topic.tab === 'analytics' ? '统计' : topic.tab === 'qa' ? '智能问答' : '数据管理' }}
              <el-icon><ArrowRight /></el-icon>
            </el-button>
            <span v-else class="restricted-note">仅管理员可进入</span>
          </div>
        </article>
      </div>
    </section>
    <ProjectTechDialog v-if="selectedTopic" :topic="selectedTopic" :is-admin="isAdmin"
                       @close="selectedTopic = null" @navigate="go" @select="openTopic" />
  </div>
</template>

<script setup>
import { defineAsyncComponent, markRaw, ref } from 'vue'
import { Monitor, Files, Cpu, DataAnalysis, MagicStick, Coin } from '@element-plus/icons-vue'
import { topics } from './projectIntroContent.js'

const props = defineProps({ isAdmin: { type: Boolean, default: false } })
const emit = defineEmits(['navigate'])
const ProjectTechDialog = defineAsyncComponent(() => import('./ProjectTechDialog.vue'))
const selectedTopic = ref(null)
const topicIcons = {
  architecture: markRaw(Monitor), documents: markRaw(Files), recognition: markRaw(Cpu),
  analytics: markRaw(DataAnalysis), qa: markRaw(MagicStick), data: markRaw(Coin),
}

const steps = [
  { number: '01', title: '上传原件', description: '保存图片或 PDF，检查格式、大小与重复内容' },
  { number: '02', title: '本地 OCR', description: '逐页处理，把图像上的文字读出来' },
  { number: '03', title: '分类与抽取', description: '按正文标签识别类型、日期与金额等字段' },
  { number: '04', title: '原件对照校对', description: '由人确认、修正，不把猜测当事实' },
  { number: '05', title: '有效版本', description: '保存本次结果与历史，供后续功能复用' },
]

function go(tab) {
  if (tab === 'data' && !props.isAdmin) return
  emit('navigate', tab)
}
function openTopic(id) { selectedTopic.value = topics.find(topic => topic.id === id) || null }
</script>

<style scoped>
.project-intro { max-width: 1240px; margin: 0 auto; padding: 2px 2px 28px; color: #193d45; }
.hero, .section-card, .feature-card { border: 1px solid #dbe6e3; border-radius: 12px; }
.hero {
  min-height: 175px; padding: 22px 30px; display: flex; align-items: center;
  justify-content: space-between; gap: 24px; overflow: hidden;
  background: linear-gradient(118deg, #e5f2ef 0%, #f8fbfa 66%, #fff 100%);
}
.hero-copy { max-width: 760px; min-width: 0; position: relative; z-index: 1; }
.eyebrow, .section-kicker { color: #367886; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.hero h1 { max-width: 720px; margin: 8px 0 10px; font-size: clamp(25px, 2.5vw, 32px); line-height: 1.32; letter-spacing: -.02em; }
.hero p, .section-heading p, .feature-card p { color: #526b71; line-height: 1.7; }
.hero p { margin: 0; font-size: 15px; }
.hero-actions { margin-top: 15px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.hero-actions .el-button + .el-button { margin-left: 0; }
.hero-actions > span { color: #526b71; font-size: 13px; line-height: 1.5; }
.hero-mark { width: 170px; height: 165px; flex: 0 0 170px; position: relative; }
.paper { position: absolute; width: 112px; height: 136px; background: #fff; border: 1px solid #bdd6d3; border-radius: 9px; box-shadow: 0 10px 28px #1d50501a; }
.paper-back { right: 10px; top: 0; transform: rotate(11deg); background: #eef6f4; }
.paper-front { left: 10px; top: 13px; transform: rotate(-6deg); padding: 35px 16px; }
.paper-front span { display: block; height: 8px; background: #d5e7e4; margin-bottom: 13px; border-radius: 5px; }
.paper-front span:last-child { width: 60%; }
.mark-check { position: absolute; right: 0; bottom: 2px; width: 57px; height: 57px; border-radius: 50%; background: #286e79; color: #fff; display: grid; place-items: center; font-size: 31px; box-shadow: 0 8px 20px #1d505033; }
.section-card { background: #fff; padding: 20px 24px; margin-top: 14px; }
.section-heading h2 { font-size: 22px; margin: 4px 0 5px; line-height: 1.35; }
.section-heading p { margin: 0; font-size: 14px; }
.workflow { list-style: none; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; padding: 0; margin: 16px 0 12px; }
.workflow-step { min-width: 0; position: relative; display: flex; flex-direction: column; gap: 5px; padding: 12px; background: #f4f8f7; border: 1px solid #dce9e6; border-radius: 9px; }
.workflow-step:not(:last-child)::after { content: '›'; position: absolute; right: -14px; top: 41%; color: #5c929a; font-size: 22px; z-index: 1; }
.step-number { color: #2e7783; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.workflow-step strong { font-size: 15px; }
.workflow-step > span:last-child { color: #5b7075; font-size: 12px; line-height: 1.55; }
.outcomes { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 13px; }
.outcomes span:not(.outcome-label) { background: #eaf4f2; border: 1px solid #d1e6e2; border-radius: 5px; padding: 5px 10px; color: #286e79; }
.outcome-label { color: #62797c; margin-right: 5px; }
.features { margin-top: 25px; }
.feature-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
.feature-card { min-width: 0; min-height: 228px; padding: 20px; background: #fff; display: flex; flex-direction: column; align-items: flex-start; }
.feature-top { display: flex; width: 100%; align-items: center; justify-content: space-between; }
.feature-number { color: #7b989a; font-size: 12px; font-weight: 700; letter-spacing: .06em; }
.feature-icon { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 9px; background: #e7f2f0; color: #286e79; font-size: 20px; }
.feature-card h3 { font-size: 17px; margin: 15px 0 7px; }
.feature-card p { margin: 0 0 12px; font-size: 13px; flex: 1; }
.feature-tech { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
.feature-tech span { padding: 3px 7px; background: #f0f6f4; color: #3f747c; border-radius: 4px; font-size: 11px; }
.feature-actions { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.feature-actions .el-button + .el-button { margin-left: 0; }
.feature-actions .el-icon { margin-left: 4px; }
.restricted-note { color: #7f9295; font-size: 12px; }
@media (max-width: 1000px) {
  .workflow { grid-template-columns: 1fr; gap: 12px; }
  .workflow-step:not(:last-child)::after { content: '↓'; right: auto; left: 50%; top: auto; bottom: -17px; font-size: 16px; }
  .feature-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 650px) {
  .hero { padding: 23px 19px; min-height: 0; }
  .hero-mark { display: none; }
  .hero h1 { font-size: 25px; }
  .hero p { font-size: 14px; }
  .hero-actions { align-items: flex-start; flex-direction: column; gap: 10px; }
  .section-card { padding: 20px 17px; margin-top: 13px; }
  .section-heading h2 { font-size: 20px; }
  .feature-grid { grid-template-columns: 1fr; }
  .feature-card { min-height: 0; }
  .features { margin-top: 20px; }
}
</style>
