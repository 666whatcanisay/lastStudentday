<template>
  <div class="app-container">
    <!-- ========== 顶部导航栏 ========== -->
    <el-header class="app-header">
      <div class="header-left">
        <el-icon class="header-icon"><Document /></el-icon>
        <span class="header-title">轻量化智能文档OCR+文本信息抽取系统</span>
        <el-tag size="small" type="info" class="version-tag">v1.0.0</el-tag>
        <el-tag size="small" type="success" effect="plain" class="version-tag">
          本地构建版 (Vite)
        </el-tag>
      </div>
      <div class="header-right">
        <el-tag :type="ocrEngine.optimized ? 'success' : 'warning'" size="small" effect="dark">
          {{ ocrEngine.name || '引擎加载中...' }}
        </el-tag>
        <el-button size="small" text style="color: #fff;" @click="showEngineInfo = true">
          <el-icon><InfoFilled /></el-icon> 引擎详情
        </el-button>
      </div>
    </el-header>

    <!-- ========== 主体内容区 ==========
         加入智能问答后，App.vue 只保留「外壳」职责：顶栏 + 页签 + 全局对话框。
         各页签的内容各自封装成组件，避免单文件膨胀到无法维护。
    -->
    <el-main class="app-main">
      <el-tabs v-model="activeTab" class="main-tabs" @tab-change="onTabChange">
        <el-tab-pane name="docs" class="pane-scroll">
          <template #label>
            <span class="tab-label"><el-icon><Files /></el-icon> 文档管理</span>
          </template>
          <DocManager ref="docManagerRef" />
        </el-tab-pane>

        <el-tab-pane name="qa" class="pane-fit">
          <template #label>
            <span class="tab-label">
              <el-icon><MagicStick /></el-icon> 智能问答
              <el-tag size="small" type="success" effect="plain" class="tab-badge">RAG</el-tag>
            </span>
          </template>
          <QaPanel ref="qaPanelRef" />
        </el-tab-pane>
      </el-tabs>
    </el-main>

    <!-- ========== OCR 引擎信息对话框 ========== -->
    <el-dialog v-model="showEngineInfo" title="OCR 引擎信息" width="500px">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="引擎名称">{{ ocrEngine.name }}</el-descriptions-item>
        <el-descriptions-item label="技术方案">{{ ocrEngine.description }}</el-descriptions-item>
        <el-descriptions-item label="轻量化优化">
          <el-tag :type="ocrEngine.optimized ? 'success' : 'warning'" size="small">
            {{ ocrEngine.optimized ? '已启用' : '未启用' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <el-divider />
      <div class="engine-tech">
        <h4>轻量化优化技术路线</h4>
        <el-steps direction="vertical" :active="4" simple>
          <el-step title="基线模型" description="PaddleOCR lightweight 官方轻量预训练模型" />
          <el-step title="模型转换" description="Paddle2ONNX 转为通用 ONNX 推理格式" />
          <el-step title="INT8 量化" description="浮点FP32 → 整型INT8，缩小模型体积" />
          <el-step title="流水线优化" description="简化推理流水线，剔除冗余算子" />
          <el-step title="性能对比" description="推理耗时降低30%+，精度下降<5%" />
        </el-steps>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { getHealth } from './api/index.js'
import DocManager from './components/DocManager.vue'
import QaPanel from './components/QaPanel.vue'

// ==================== 页签 ====================
const activeTab = ref('docs')
const docManagerRef = ref(null)
const qaPanelRef = ref(null)

/**
 * 切页签时刷新对侧数据 —— 两侧存在真实的数据耦合：
 *   · 在「文档管理」上传/识别了文档 → 问答侧需要知道有新文档待索引
 *   · 在「智能问答」建了索引        → 文档管理侧不需要变，但刷新无副作用
 * 不刷新会导致「刚传的文档检索不到」这种让人困惑的现象。
 */
function onTabChange(name) {
  if (name === 'qa') qaPanelRef.value?.refresh()
  else docManagerRef.value?.refresh()
}

// ==================== OCR 引擎信息 ====================
const showEngineInfo = ref(false)
const ocrEngine = reactive({ name: '', description: '', optimized: false })

async function fetchHealth() {
  try {
    const res = await getHealth()
    ocrEngine.name = res.data.ocr_engine.name
    ocrEngine.description = res.data.ocr_engine.description
    ocrEngine.optimized = res.data.ocr_engine.optimized
  } catch (e) {
    // ignore
  }
}

onMounted(() => {
  fetchHealth()
})
</script>

<style scoped>
/* ========== 全局布局 ==========
   用弹性布局把「视口高度」层层传递下去，而不是在各处写 calc(100vh - Npx) 的魔数。
   魔数写法的问题是：顶栏高度、页签高度、内边距任何一处调整，都要重新配平所有数字，
   一旦配不平就溢出视口 —— 实测出现过输入区被截断、页面多出一条滚动条。
   现在的做法：容器固定 100vh 且 overflow:hidden，滚动交给最内层需要滚动的区域。 */
.app-container {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #f0f2f5;
}

/* ========== 导航栏 ========== */
.app-header {
  background: linear-gradient(135deg, #409EFF 0%, #36cfc9 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.header-icon { font-size: 22px; }
.header-title {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 1px;
}
.version-tag {
  background: rgba(255, 255, 255, 0.2);
  border-color: rgba(255, 255, 255, 0.3);
  color: #fff;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* ========== 主内容 ========== */
.app-main {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 1500px;
  margin: 0 auto;
  padding: 12px 20px 16px;
  box-sizing: border-box;
}

/* ========== 页签 ==========
   把 el-tabs 也变成纵向弹性容器，页签内容区才能撑满剩余高度。
   文档管理页内容可能很长，滚动交给 el-tab-pane 自己；
   问答页高度自适应，不需要滚动。 */
.main-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.main-tabs :deep(.el-tabs__header) { margin-bottom: 12px; flex-shrink: 0; }
.main-tabs :deep(.el-tabs__item) { font-size: 14px; }
.main-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
/* 文档管理页内容可能很长 → 自己滚；问答页自己管内部滚动 → 外层不滚。
   用显式类名而不是 :has()，避免依赖较新的选择器支持。 */
.main-tabs :deep(.pane-scroll) {
  height: 100%;
  overflow-y: auto;
}
.main-tabs :deep(.pane-fit) {
  height: 100%;
  overflow: hidden;
}
.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.tab-badge {
  transform: scale(0.82);
  margin-left: 2px;
}

/* ========== 引擎信息 ========== */
.engine-tech h4 { margin-bottom: 12px; }
</style>
