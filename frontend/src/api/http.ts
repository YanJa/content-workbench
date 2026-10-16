/**
 * HTTP 客户端封装。
 * 统一 baseURL、超时、错误处理。
 */
import axios, { type AxiosInstance } from 'axios'

import { getApiBase } from './config'
import { clearToken, getToken } from './token'

export const http: AxiosInstance = axios.create({
  baseURL: getApiBase(),
  timeout: 60000,
})

// 用户可能在「设置」页改了后端地址，每次请求前重新取一次，
// 这样改完立即生效，不用刷新页面。
// 同时带 Authorization: Bearer <JWT>：用户模块上线后，数据归属由
// 服务端从登录态解析（见 backend/app/core/tenant.py），客户端不再自称。
http.interceptors.request.use((config) => {
  config.baseURL = getApiBase()
  const token = getToken()
  if (token) config.headers['Authorization'] = `Bearer ${token}`
  return config
})

/** 登录/注册页自身的 401 不触发跳转（否则登录失败也会被弹去登录页，死循环） */
function isAuthPage(): boolean {
  return /^\/(login|register)(\?|$)/.test(window.location.pathname)
}

/** token 失效：清掉并回登录页。动态 import router 是为了打断
 *  「http → router → stores → api → http」的静态循环依赖。 */
async function handleUnauthorized(): Promise<void> {
  clearToken()
  if (isAuthPage()) return
  const redirect = encodeURIComponent(window.location.pathname + window.location.search)
  const { default: router } = await import('@/router')
  void router.replace(`/login?redirect=${redirect}`)
}

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error?.response?.status
    const detail =
      error?.response?.data?.detail ||
      error?.response?.data?.message ||
      error?.message ||
      '请求失败'
    if (status === 401) void handleUnauthorized()
    console.error('[API]', detail, error?.config?.url)
    return Promise.reject(new Error(detail))
  },
)

/** 后端探活 */
export async function checkHealth() {
  const { data } = await http.get('/health')
  return data as {
    status: string
    env: string
    llm_configured: boolean
    redis: boolean
    tools_enabled: boolean
  }
}
