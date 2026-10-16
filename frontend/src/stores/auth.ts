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

import { getMe, login as apiLogin, register as apiRegister, type AuthUser } from '@/api/auth'
import { clearToken, getToken, setToken } from '@/api/token'

export type { AuthUser }

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  /** bootstrap 是否已完成（路由守卫据此决定放行还是重定向） */
  const bootstrapped = ref(false)

  const isAuthenticated = computed(() => user.value !== null)

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

  async function register(username: string, password: string, inviteCode = ''): Promise<void> {
    const res = await apiRegister(username, password, inviteCode)
    setToken(res.token)
    user.value = res.user
  }

  function logout(): void {
    clearToken()
    user.value = null
  }

  return { user, bootstrapped, isAuthenticated, bootstrap, login, register, logout }
})
