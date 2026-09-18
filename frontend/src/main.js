/**
 * Vue3 应用入口
 * 本地构建版 — Element Plus 按需引入
 */
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'

const app = createApp(App)

// 注册 Element Plus（中文语言包）
app.use(ElementPlus, { locale: zhCn })

// 注册所有图标组件
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
}

app.mount('#app')
