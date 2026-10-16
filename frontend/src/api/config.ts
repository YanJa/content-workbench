/**
 * 后端地址配置。
 *
 * 为什么需要这个：
 *   默认前端和后端同源部署（/api 由 Vite 或 nginx 代理），不需要配置。
 *   但通过静态文件通道打开前端时（如 sandbox-preview），页面 origin 和后端
 *   不在一处，/api 请求会打到静态服务器并返回 404。
 *   这时用户可以在「设置」页手动填后端地址，存在 localStorage 里。
 *
 * 取值优先级：
 *   1. localStorage 里用户填的地址（设置页可改）
 *   2. 构建期环境变量 VITE_API_BASE
 *   3. 默认 '/api'
 */
const STORAGE_KEY = 'cwb_api_base'

const DEFAULT_BASE = import.meta.env.VITE_API_BASE || '/api'

/** 规范化用户输入的地址：补协议、去掉末尾斜杠、确保带 /api 前缀 */
export function normalizeApiBase(raw: string): string {
  let s = raw.trim()
  if (!s) return DEFAULT_BASE

  // 手输 127.0.0.1:8000 这种没带协议的，按 http 处理
  if (!/^https?:\/\//i.test(s)) {
    s = `http://${s}`
  }
  // 去掉末尾斜杠
  s = s.replace(/\/+$/, '')

  // 没写 /api 的自动补上（后端所有路由都挂在 /api 下）
  if (!/\/api$/i.test(s)) {
    s = `${s}/api`
  }
  return s
}

/** 当前生效的 API 基地址 */
export function getApiBase(): string {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && saved.trim()) return saved
  } catch {
    // 隐私模式下 localStorage 可能抛异常，忽略即可
  }
  return DEFAULT_BASE
}

/** 保存用户自定义的后端地址，传空字符串恢复默认 */
export function setApiBase(raw: string): string {
  const value = raw.trim() ? normalizeApiBase(raw) : DEFAULT_BASE
  try {
    if (raw.trim()) {
      localStorage.setItem(STORAGE_KEY, value)
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  } catch {
    // 存不进去也继续用当前值，不阻塞调用方
  }
  return value
}

/** 是否使用了自定义地址（设置页展示用） */
export function isCustomApiBase(): boolean {
  return getApiBase() !== DEFAULT_BASE
}
