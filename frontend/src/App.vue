<template>
  <div class="app-container">
    <!-- ========== 顶部导航栏 ========== -->
    <el-header class="app-header">
      <div class="header-left">
        <el-icon class="header-icon"><Document /></el-icon>
        <span class="header-title">智能文档工作台</span>
      </div>
      <div class="header-right">
        <el-tag v-if="auth.demoMode && auth.authenticated" size="small" effect="dark">
          {{ auth.role === 'admin' ? '管理员' : '受限访问' }}
        </el-tag>
        <el-button v-if="auth.demoMode && auth.authenticated" size="small" text style="color: #fff;" @click="signOut">
          退出登录
        </el-button>
        <el-button v-if="auth.authenticated" size="small" text class="header-link" @click="showEngineInfo = true">
          <el-icon><InfoFilled /></el-icon> 系统信息
        </el-button>
      </div>
    </el-header>

    <!-- ========== 主体内容区 ==========
         加入智能问答后，App.vue 只保留「外壳」职责：顶栏 + 页签 + 全局对话框。
         各页签的内容各自封装成组件，避免单文件膨胀到无法维护。
    -->
    <el-main class="app-main">
      <div v-if="!auth.ready" class="auth-state">正在检查访问状态…</div>
      <div v-else-if="auth.demoMode && !auth.authenticated" class="auth-state">
        <el-card class="login-card" shadow="hover">
          <h2>访问登录</h2>
          <p>请输入访问口令。登录后可完整使用文档识别、校对、统计、导出与智能问答。</p>
          <el-form @submit.prevent="signIn">
            <el-form-item>
              <el-input v-model="password" type="password" show-password
                        autocomplete="current-password" placeholder="访问口令" @keyup.enter="signIn" />
            </el-form-item>
            <el-button type="primary" native-type="submit" :loading="signingIn" style="width: 100%;">
              登录
            </el-button>
          </el-form>
        </el-card>
      </div>
      <el-tabs v-else v-model="activeTab" class="main-tabs">
        <el-tab-pane name="docs" class="pane-scroll">
          <template #label>
            <span class="tab-label"><el-icon><Files /></el-icon> 文档管理</span>
          </template>
          <DocManager :is-admin="auth.role === 'admin'" />
        </el-tab-pane>

        <el-tab-pane name="analytics" class="pane-scroll" lazy>
          <template #label>
            <span class="tab-label"><el-icon><DataAnalysis /></el-icon> 统计</span>
          </template>
          <AnalyticsPanel />
        </el-tab-pane>

        <el-tab-pane name="qa" class="pane-fit" lazy>
          <template #label>
            <span class="tab-label">
              <el-icon><MagicStick /></el-icon> 智能问答
            </span>
          </template>
          <QaPanel :is-admin="auth.role === 'admin'" />
        </el-tab-pane>

        <el-tab-pane v-if="auth.role === 'admin'" name="data" class="pane-scroll" lazy>
          <template #label>
            <span class="tab-label"><el-icon><Coin /></el-icon> 数据管理</span>
          </template>
          <DataManager />
        </el-tab-pane>
      </el-tabs>
    </el-main>

    <!-- ========== OCR 引擎信息对话框 ========== -->
    <el-dialog v-model="showEngineInfo" title="系统信息" width="500px">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="版本">v{{ systemVersion }} · 本地构建版</el-descriptions-item>
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
          <el-step title="推理格式" description="使用已发布的 ONNX 模型进行本地推理" />
          <el-step title="本地模型" description="OCR 使用本地 ONNX 模型，向量模型使用 INT8" />
          <el-step title="流水线优化" description="简化推理流水线，剔除冗余算子" />
          <el-step title="实际性能" description="以当前机器、文档和测试记录为准" />
        </el-steps>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, defineAsyncComponent } from 'vue'
import { ElMessage } from 'element-plus'
import { getHealth, getAuthStatus, loginDemo, logoutDemo } from './api/index.js'
import DocManager from './components/DocManager.vue'
const AnalyticsPanel = defineAsyncComponent(() => import('./components/AnalyticsPanel.vue'))
const QaPanel = defineAsyncComponent(() => import('./components/QaPanel.vue'))
const DataManager = defineAsyncComponent(() => import('./components/DataManager.vue'))

// ==================== 页签 ====================
const activeTab = ref('docs')
const auth = reactive({ ready: false, demoMode: false, authenticated: false, role: null })
const password = ref('')
const signingIn = ref(false)

function applyAuth(data) {
  auth.demoMode = Boolean(data.demo_mode)
  auth.authenticated = Boolean(data.authenticated)
  auth.role = data.role || null
  auth.ready = true
  if (auth.role !== 'admin' && activeTab.value === 'data') activeTab.value = 'docs'
}

async function loadAuth() {
  try {
    const result = await getAuthStatus()
    applyAuth(result.data)
    if (auth.authenticated) fetchHealth()
  } catch (e) {
    auth.ready = true
    auth.authenticated = false
    ElMessage.error('无法确认登录状态，请检查本机服务')
  }
}

async function signIn() {
  if (!password.value || signingIn.value) return
  signingIn.value = true
  try {
    const result = await loginDemo(password.value)
    password.value = ''
    applyAuth(result.data)
    fetchHealth()
  } catch (e) {
    password.value = ''
  } finally {
    signingIn.value = false
  }
}

async function signOut() {
  try {
    const result = await logoutDemo()
    applyAuth(result.data)
    activeTab.value = 'docs'
  } catch (e) {
    ElMessage.error('退出失败，请重试')
  }
}

function authExpired() {
  if (auth.demoMode) {
    auth.authenticated = false
    auth.role = null
    activeTab.value = 'docs'
  }
}

// ==================== OCR 引擎信息 ====================
const showEngineInfo = ref(false)
const systemVersion = ref('未知')
// optimized: null = 尚未取到引擎信息（未知）。不要用 false 作默认值 ——
// 那样首屏会先把「未知」误显示成「模拟引擎 (Mock)」闪一下。
const ocrEngine = reactive({ name: '', description: '', optimized: null })

async function fetchHealth() {
  try {
    const res = await getHealth()
    systemVersion.value = res.data.version || '未知'
    ocrEngine.name = res.data.ocr_engine.name
    ocrEngine.description = res.data.ocr_engine.description
    ocrEngine.optimized = res.data.ocr_engine.optimized
  } catch (e) {
    // ignore
  }
}

onMounted(() => {
  window.addEventListener('doc-ocr-auth-expired', authExpired)
  loadAuth()
})
onUnmounted(() => window.removeEventListener('doc-ocr-auth-expired', authExpired))
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
  background: #f4f7f6;
}

/* ========== 导航栏 ========== */
.app-header {
  background: #173c44;
  color: #f8fcfa;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
  border-bottom: 1px solid #dce7e4;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.header-icon { font-size: 21px; color: #93d4ce; }
.header-title {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: .3px;
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
.header-link { color: #f8fcfa; }

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
.auth-state { flex: 1; display: flex; align-items: center; justify-content: center; color: #526781; }
.login-card { width: min(440px, 100%); }
.login-card h2 { margin: 0 0 12px; color: #19344f; }
.login-card p { color: #526781; line-height: 1.6; margin-bottom: 24px; }

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
   用显式类名而不是 :has()，避免依赖较新的选择器支持。
   注意 overflow-x:hidden 是必需的：el-row 的栅格用负外边距实现 gutter
   （gutter=16 → 左右各 -8px），行盒比容器宽 16px；
   而只写 overflow-y:auto 时 overflow-x 会按规范一并变成 auto，
   于是页面底部多出一条无意义的横向滚动条。
   隐藏不会裁掉可见内容 —— 列自身有 8px 内边距，卡片正好对齐容器边缘。 */
.main-tabs :deep(.pane-scroll) {
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
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

/* ========== 引擎信息 ========== */
.engine-tech h4 { margin-bottom: 12px; }

/* ========== 窄屏 ==========
   实测 700px 宽时顶栏标题会折成两行，把右侧标签挤变形。
   标题必须单行截断，次要的版本标签直接让位。 */
@media (max-width: 900px) {
  .header-left { min-width: 0; }
  .header-title {
    font-size: 15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .version-tag { display: none; }
}
@media (max-width: 600px) {
  .app-header { height: 50px; padding: 0 12px; }
  .header-title { font-size: 14px; }
  .header-right { gap: 3px; }
  .header-right :deep(.el-tag) { display: none; }
  .header-right :deep(.el-button) { padding: 4px; font-size: 12px; }
  .app-main { padding: 8px 10px 10px; }
  .main-tabs :deep(.el-tabs__nav-wrap) { overflow: auto; }
  .main-tabs :deep(.el-tabs__nav-wrap::after) { display: none; }
  .main-tabs :deep(.el-tabs__item) { padding: 0 10px; font-size: 13px; }
  .tab-label { gap: 3px; }
}
</style>
