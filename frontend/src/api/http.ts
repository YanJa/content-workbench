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

/** 403 权限不足 / 429 配额用尽：拉一次 /auth/me 把前端的权限与用量纠正到
 * 服务端的真实值。动态 import store 同理是为了打断循环依赖。
 *
 * 为什么必须刷：管理员可能刚把这个人的权限收了、或者额度改了，前端
 * /auth/me 还是一分钟前的快照。不刷新的话按钮会继续亮着，用户点了还是
 * 403，来回几次就成了「这站点是不是坏了」。
 *
 * 只刷一次就够：连续失败（比如狂点发送）不会打出请求风暴，60 秒的静默
 * 定时刷新会兜住后续。 */
let permRefreshPending = false
async function refreshPermSnapshot(): Promise<void> {
  if (permRefreshPending) return
  permRefreshPending = true
  try {
    const { useAuthStore } = await import('@/stores/auth')
    await useAuthStore().refreshMe()
  } catch {
    /* 刷新失败无所谓：真正的拦截已经在服务端发生了 */
  } finally {
    permRefreshPending = false
  }
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
    // 403 有两种：角色不够（管理面）和功能权限不够（按钮级）。文案后端都给了，
    // 这里不区分，只负责把快照刷成服务端的真相。
    if (status === 403 || status === 429) void refreshPermSnapshot()
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
