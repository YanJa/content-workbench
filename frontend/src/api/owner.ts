/**
 * 数据归属（owner）配置。
 *
 * 用户模块落地前的隔离口径：所有请求带 `X-Owner-Id` 头，后端按它过滤
 * 数据。默认 "default"（单租户，兼容存量数据与 curl）。
 *
 * 用户体系上线后：这个模块存的值会被登录态取代 —— 后端改成只认服务端
 * 解析出的身份（见 backend/app/core/tenant.py 注释），前端这里可以
 * 直接返回固定值或删掉。
 *
 * 为什么存 localStorage：和 api/config.ts 的后端地址一个思路，
 * 用户在设置页改完立即生效，不用刷新页面。
 */
const STORAGE_KEY = 'cwb_owner_id'

export const DEFAULT_OWNER = 'default'

/** 规范化：只放行后端认的字符（1~64 位字母数字 ._-），非法回落默认值 */
export function normalizeOwner(raw: string): string {
  const v = (raw || '').trim()
  if (!v) return DEFAULT_OWNER
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(v)) return DEFAULT_OWNER
  return v
}

/** 当前生效的数据归属 */
export function getOwnerId(): string {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && saved.trim()) return normalizeOwner(saved)
  } catch {
    // 隐私模式下 localStorage 可能抛异常，忽略即可
  }
  return DEFAULT_OWNER
}

/** 保存自定义归属，传空字符串恢复默认 */
export function setOwnerId(raw: string): string {
  const value = raw.trim() ? normalizeOwner(raw) : DEFAULT_OWNER
  try {
    if (value === DEFAULT_OWNER) {
      localStorage.removeItem(STORAGE_KEY)
    } else {
      localStorage.setItem(STORAGE_KEY, value)
    }
  } catch {
    // 存不进去也继续用当前值，不阻塞调用方
  }
  return value
}

/** 是否使用了非默认归属（设置页展示用） */
export function isCustomOwner(): boolean {
  return getOwnerId() !== DEFAULT_OWNER
}
