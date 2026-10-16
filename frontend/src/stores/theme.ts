/**
 * 主题 Store：浅色 / 深色 / 跟随系统。
 *
 * - 选择存 localStorage（cwb_theme），刷新不丢
 * - 首次访问（没存过）= 跟随系统
 * - resolved 是最终生效的主题，通过 <html data-theme> 落到 CSS 变量，
 *   同时 App.vue 用它切换 Naive UI 的 theme
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'cwb_theme'

function readStored(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    // 隐私模式下 localStorage 可能抛异常，按系统偏好走
  }
  return 'system'
}

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-color-scheme: dark)').matches
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(readStored())
  const systemDark = ref(systemPrefersDark())

  /** 最终生效的主题 */
  const resolved = computed<'light' | 'dark'>(() =>
    mode.value === 'system' ? (systemDark.value ? 'dark' : 'light') : mode.value,
  )

  const isDark = computed(() => resolved.value === 'dark')

  /** 把主题写到 <html data-theme>，CSS 变量随之切换 */
  function apply(): void {
    if (typeof document === 'undefined') return
    document.documentElement.dataset.theme = resolved.value
  }

  /** 监听系统主题变化（仅 system 模式下有意义，但常驻监听更简单） */
  function watchSystem(): void {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', (e) => {
      systemDark.value = e.matches
      apply()
    })
  }

  function setMode(m: ThemeMode): void {
    mode.value = m
    try {
      if (m === 'system') {
        localStorage.removeItem(STORAGE_KEY)
      } else {
        localStorage.setItem(STORAGE_KEY, m)
      }
    } catch {
      // 存不进去不影响当前会话
    }
    apply()
  }

  /** 头部按钮用：在浅色/深色之间显式切换（退出 system 模式） */
  function toggle(): void {
    setMode(resolved.value === 'dark' ? 'light' : 'dark')
  }

  return { mode, resolved, isDark, setMode, toggle, apply, watchSystem }
})
