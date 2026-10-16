/**
 * 登录令牌的本地存储。
 *
 * 与 stores/auth.ts 共用同一把键（cwb_token）：api 层（http.ts / chat.ts）
 * 在请求拦截器里同步读它带头部，store 在登录/登出时写/清它。
 * 不 import store 是为了避免「api ← store ← api」的循环依赖。
 *
 * 另：顺手清理用户模块上线前留下的旧键 cwb_owner_id（当年的「数据归属」，
 * 已由服务端登录身份取代，留着只会让人误解它还有用）。
 */
const TOKEN_KEY = 'cwb_token'
const LEGACY_OWNER_KEY = 'cwb_owner_id'

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    // 隐私模式下 localStorage 可能抛异常
    return null
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // 存不进去不影响本次会话（内存里还有 token），刷新后才需要重新登录
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(LEGACY_OWNER_KEY)
  } catch {
    // 忽略
  }
}

/** 模块加载时清一次旧键（幂等，之后 clearToken 也会带） */
try {
  localStorage.removeItem(LEGACY_OWNER_KEY)
} catch {
  // 忽略
}
