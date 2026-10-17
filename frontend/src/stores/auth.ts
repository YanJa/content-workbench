/**
 * 登录态 Store。
 *
 * - token 存 localStorage（cwb_token，见 api/token.ts），刷新不丢；
 * - bootstrap() 在应用挂载前调用：有 token 就找后端验一下（/auth/me），
 *   过期/失效则清掉走登录页 —— 避免「刷新页面先闪一下工作台再被踢走」；
 * - user 只存后端返回的最小信息（id/用户名/注册时间）。
 *
 * 细粒度权限（2026-10 上线）
 * --------------------------
 * `/auth/me` 现在一并返回 `permissions` / `quotas` / `usage`，本 store 把它们
 * 加工成三个查询函数给全站用：
 *
 * - `can(key)` —— 某个权限点有没有。**按钮显隐/置灰的唯一依据**；
 * - `quotaOf(feature)` / `quotaLeft(feature)` / `quotaExhausted(feature)`；
 * - `markUsed(feature)` —— 动作成功后的乐观置灰。
 *
 * 三个「为什么」：
 *
 * 1. **为什么不在前端做权限控制。** 这里算出来的东西全是 UX 便利（少弹一个
 *    403、少一个灰不了的按钮）。真正的校验在后端每个端点的依赖上，前端
 *    藏起来的按钮照样能被人拿 curl 打过来。
 * 2. **为什么要 60 秒静默刷新。** 管理员刚改了某个人的权限/配额，被改的
 *    人页面还开着。不能实时推送（没有 WS 基础设施），也不能只等用户自己
 *    刷新页面 —— 那就成了「改完得通知对方重启浏览器」。一分钟的延迟对
 *    「今天对话次数用完了」这种场景完全可接受。
 * 3. **为什么要乐观置灰。** 配额超限时后端返回 429，但失败反馈走的是
 *    message 提示，按钮不会自己灰。发一次对话就本地 used++，用户立刻看到
 *    「还剩 3 次」往下掉；万一请求其实失败了，拦截器里的 refreshMe() 会
 *    用服务端的真实值纠正回来，不会一直错下去。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getMe,
  login as apiLogin,
  register as apiRegister,
  type AuthUser,
  type ClaimedSummary,
  type QuotaUsage,
  type Role,
} from '@/api/auth'
import { clearToken, getToken, setToken } from '@/api/token'

export type { AuthUser, ClaimedSummary, QuotaUsage, Role }

/** 静默刷新间隔：分钟级够用，且不至于把 /auth/me 打成热点 */
const REFRESH_INTERVAL_MS = 60_000

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  /** bootstrap 是否已完成（路由守卫据此决定放行还是重定向） */
  const bootstrapped = ref(false)

  const isAuthenticated = computed(() => user.value !== null)
  /**
   * 是否管理员。角色来自 /auth/me，每次 bootstrap 都重取 —— 不缓存进本地，
   * 也不从 token 里解（token 是 7 天有效的 JWT，把角色签进去就得等 7 天
   * 才生效；见后端 core/rbac.py 的说明）。
   *
   * 注意这只用来控制菜单显隐和提前跳转，**不是权限控制** —— 真正的校验
   * 在后端每个管理端点上。
   */
  const isAdmin = computed(() => user.value?.role === 'admin')

  // ---------------------------------------------------------------- 权限
  /** 权限点集合。用 Set 而不是数组 includes：菜单渲染每次都查，O(1) 舒服点 */
  const permSet = computed<Set<string>>(() => new Set(user.value?.permissions ?? []))

  /**
   * 有没有某个权限点。admin 恒为 true（后端也是这样：管理员权限固定全开，
   * 不查表、不受勾选影响，防的就是「把自己勾空」这种自锁）。
   */
  function can(key: string): boolean {
    if (!user.value) return false
    if (user.value.role === 'admin') return true
    return permSet.value.has(key)
  }

  // ---------------------------------------------------------------- 配额
  const usage = computed<Record<string, QuotaUsage>>(() => user.value?.usage ?? {})
  const quotas = computed(() => user.value?.quotas ?? {})

  function quotaOf(feature: string): QuotaUsage | undefined {
    return usage.value[feature]
  }

  /** 剩余次数。null = 不限（别用 0 表示不限，那是「用完了」） */
  function quotaLeft(feature: string): number | null {
    const snap = usage.value[feature]
    if (!snap) return null
    return snap.unlimited ? null : snap.left
  }

  /** 是否已用尽。不限的功能永远 false */
  function quotaExhausted(feature: string): boolean {
    const snap = usage.value[feature]
    if (!snap || snap.unlimited) return false
    return snap.left !== null && snap.left <= 0
  }

  /**
   * 乐观置灰：动作成功发出后本地 used++。
   *
   * 只动内存里的 user 对象，**不写 localStorage** —— 刷新页面时以服务端
   * 为准，本地这个增量只是为了让按钮立刻有反馈。
   */
  function markUsed(feature: string): void {
    const u = user.value
    if (!u?.usage?.[feature]) return
    const snap = u.usage[feature]
    snap.used += 1
    if (snap.limit !== null) snap.left = Math.max(0, snap.limit - snap.used)
    // 触发响应式：原地改嵌套对象不会通知 computed，整体赋一次值
    u.usage = { ...u.usage }
  }

  // ---------------------------------------------------------------- 刷新
  /**
   * 重新拉一次 /auth/me。静默失败（不弹提示、不跳转）——
   * 调用方全是「顺手刷新一下」的场景，401 由 http 拦截器统一处理。
   */
  async function refreshMe(): Promise<void> {
    if (!getToken()) return
    try {
      user.value = await getMe()
    } catch {
      /* 静默：网络抖动不该打断用户正在做的事 */
    }
  }

  let refreshTimer: ReturnType<typeof setInterval> | null = null
  let visibilityBound = false

  function startAutoRefresh(): void {
    if (refreshTimer) return
    refreshTimer = setInterval(() => void refreshMe(), REFRESH_INTERVAL_MS)

    // 切回标签页时立刻刷一次：用户很可能刚在别的页面被管理员改了额度，
    // 或者离开期间正好跨了零点重置。visibilitychange 比定时器更懂「用户回来了」。
    if (!visibilityBound && typeof document !== 'undefined') {
      visibilityBound = true
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') void refreshMe()
      })
    }
  }

  function stopAutoRefresh(): void {
    if (refreshTimer) {
      clearInterval(refreshTimer)
      refreshTimer = null
    }
  }

  async function bootstrap(): Promise<void> {
    if (!getToken()) {
      bootstrapped.value = true
      return
    }
    try {
      user.value = await getMe()
      startAutoRefresh()
    } catch {
      clearToken()
      user.value = null
    } finally {
      bootstrapped.value = true
    }
  }

  async function login(username: string, password: string): Promise<void> {
    const res = await apiLogin(username, password)
    setToken(res.token)
    user.value = res.user
    startAutoRefresh()
  }

  /**
   * 注册。返回首用户的存量认领摘要（非首用户恒为 null），
   * 由调用方决定是否提示 —— store 不碰 UI。
   */
  async function register(
    username: string,
    password: string,
    inviteCode = '',
  ): Promise<ClaimedSummary | null> {
    const res = await apiRegister(username, password, inviteCode)
    setToken(res.token)
    user.value = res.user
    startAutoRefresh()
    return res.claimed ?? null
  }

  function logout(): void {
    stopAutoRefresh()
    clearToken()
    user.value = null
  }

  return {
    user,
    bootstrapped,
    isAuthenticated,
    isAdmin,
    usage,
    quotas,
    can,
    quotaOf,
    quotaLeft,
    quotaExhausted,
    markUsed,
    refreshMe,
    startAutoRefresh,
    stopAutoRefresh,
    bootstrap,
    login,
    register,
    logout,
  }
})
