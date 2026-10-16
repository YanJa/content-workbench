/**
 * 登录态 Store。
 *
 * - token 存 localStorage（cwb_token，见 api/token.ts），刷新不丢；
 * - bootstrap() 在应用挂载前调用：有 token 就找后端验一下（/auth/me），
 *   过期/失效则清掉走登录页 —— 避免「刷新页面先闪一下工作台再被踢走」；
 * - user 只存后端返回的最小信息（id/用户名/注册时间）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getMe,
  login as apiLogin,
  register as apiRegister,
  type AuthUser,
  type ClaimedSummary,
  type Role,
} from '@/api/auth'
import { clearToken, getToken, setToken } from '@/api/token'

export type { AuthUser, ClaimedSummary, Role }

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

  async function bootstrap(): Promise<void> {
    if (!getToken()) {
      bootstrapped.value = true
      return
    }
    try {
      user.value = await getMe()
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
    return res.claimed ?? null
  }

  function logout(): void {
    clearToken()
    user.value = null
  }

  return {
    user,
    bootstrapped,
    isAuthenticated,
    isAdmin,
    bootstrap,
    login,
    register,
    logout,
  }
})
