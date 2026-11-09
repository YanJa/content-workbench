import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // 备案号属于站点主体的私有信息，不写进仓库，只在构建时注入（见 src/utils/icp.ts）。
  // 生产构建缺这一项，页面底部就不会有备案号 —— 境内公网部署属于合规风险，这里出声提醒。
  if (mode === 'production' && !env.VITE_ICP_BEIAN?.trim()) {
    console.warn(
      '\n[cwb] 未设置 VITE_ICP_BEIAN：备案号页脚不会渲染。\n' +
        '     境内公网部署请在 frontend/.env 补齐后重新构建（该文件不进 git）。\n' +
        '     本地开发 / 境外部署可忽略。\n',
    )
  }

  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: 5173,
      host: true,
      // Vite 6 默认拒绝非 localhost 的 Host（防 DNS rebinding），
      // 经反向隧道/公网域名访问时需要显式放行。
      // 具体域名走 VITE_ALLOWED_HOSTS（写在自己机器的 frontend/.env 里，
      // 不进仓库 —— 公网域名和 IP 属于部署方信息，不随开源分发）。
      allowedHosts: (env.VITE_ALLOWED_HOSTS ?? 'localhost,127.0.0.1').split(','),
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
  }
})
