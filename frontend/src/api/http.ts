/**
 * HTTP 客户端封装。
 * 统一 baseURL、超时、错误处理。
 */
import axios, { type AxiosInstance } from 'axios'

import { getApiBase } from './config'
import { getOwnerId } from './owner'

export const http: AxiosInstance = axios.create({
  baseURL: getApiBase(),
  timeout: 60000,
})

// 用户可能在「设置」页改了后端地址，每次请求前重新取一次，
// 这样改完立即生效，不用刷新页面。
// 同时带上 X-Owner-Id：后端按它做数据隔离（用户模块落地前的接缝，
// 见 backend/app/core/tenant.py）。
http.interceptors.request.use((config) => {
  config.baseURL = getApiBase()
  config.headers['X-Owner-Id'] = getOwnerId()
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const detail =
      error?.response?.data?.detail ||
      error?.response?.data?.message ||
      error?.message ||
      '请求失败'
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
