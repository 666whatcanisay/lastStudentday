import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    // 开发时代理后端API，避免跨域
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  base: '/vite/',  // 构建资源的基础路径，与后端 mount 路径一致
  build: {
    // 构建输出到后端的 static_vite 目录，FastAPI 通过 /vite/ 路径提供服务
    outDir: '../app/static_vite',
    emptyOutDir: true,
  },
})
