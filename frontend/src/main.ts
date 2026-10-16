import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'
import './styles/main.css'
import './styles/theme.css'

const app = createApp(App)
app.use(createPinia())

// 挂载前应用一次主题（index.html 里的内联脚本已同步设置过 data-theme，
// 这里让 store 接管，并开始监听系统主题变化）
const theme = useThemeStore()
theme.apply()
theme.watchSystem()

// 先恢复登录态再挂载：避免「刷新 → 守卫把用户踢到登录页 → bootstrap
// 完成发现其实已登录」的闪烁。bootstrap 只读本地 token 并验一次 /auth/me。
async function boot() {
  const auth = useAuthStore()
  await auth.bootstrap()
  app.use(router)
  app.mount('#app')
}

void boot()
