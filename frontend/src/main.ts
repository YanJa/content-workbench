import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
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

app.use(router)
app.mount('#app')
