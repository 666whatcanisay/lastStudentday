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
    // ⚠️ 刻意不设 host，保持默认的 localhost —— Windows 上 Node 会把它解析成
    // IPv6 的 ::1，于是 dev server **只监听 [::1]:5173**，`127.0.0.1:5173` 连不上。
    //   · 浏览器访问 http://localhost:5173/vite/ 正常（文档里写的也是 localhost）
    //   · 但用 curl / 脚本探测时**必须写 localhost**，写 127.0.0.1 会得到 000
    //     —— 曾因此误判「Vite 没起来」，空等了 80 秒才去读日志
    // 不要改成 host: '127.0.0.1'（那样 localhost 可能反而解析不到）；
    // 也不要改成 host: true（会暴露到局域网，而 .env 里有 LLM Key）。
  },
  base: '/vite/',  // 构建资源的基础路径，与后端 mount 路径一致
  build: {
    // 构建输出到后端的 static_vite 目录，FastAPI 通过 /vite/ 路径提供服务
    outDir: '../app/static_vite',
    emptyOutDir: true,
  },
})
