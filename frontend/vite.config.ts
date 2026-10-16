import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    host: true,
    // Vite 6 默认拒绝非 localhost 的 Host（防 DNS rebinding），
    // 经反向隧道/公网域名访问时需要显式放行。
    // 可用环境变量 VITE_ALLOWED_HOSTS 覆盖，逗号分隔。
    allowedHosts: (process.env.VITE_ALLOWED_HOSTS ?? 'cwb.draftpaper.ltd,49.233.223.252').split(','),
    // 开发期把 /api 代理到后端，避免跨域
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 单文件超过 500KB 会让首屏变慢，把体积大的依赖单独拆包
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia'],
          naive: ['naive-ui'],
        },
      },
    },
  },
})
