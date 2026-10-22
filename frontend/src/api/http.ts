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

// 数组参数用 repeat 格式（key=a&key=b），而不是 axios 默认的 key[]=a&key[]=b。
// 后者 FastAPI 的 `list[str] = Query(...)` 读不到（它只认重复的 key），会导致
// 像 /knowledge/origin-check?content_ids=... 这种批量接口拿不到数组。
// 标量参数（绝大多数 GET）两种格式等价，所以这个改动对现有接口无副作用。
http.defaults.paramsSerializer = {
  serialize: (params) =>
    new URLSearchParams(
      Object.entries(params).flatMap(([k, v]) => {
        if (v == null) return []
        if (Array.isArray(v)) {
          return v.map((x) => [k, x == null ? '' : String(x)]) as [string, string][]
        }
        return [[k, String(v)]] as [string, string][]
      }),
    ).toString(),
}

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

/** 从错误响应体里抠后端文案：兼容 JSON 对象与 JSON 字符串（blob 转来的）两种形态。 */
function pickDetail(data: any): string | undefined {
  if (typeof data === 'string') {
    try {
      return JSON.parse(data)?.detail
    } catch {
      return undefined
    }
  }
  return data?.detail || data?.message
}

/** 按状态码做副作用：
 *  401 → 清 token 回登录页；
 *  403 → 角色不够（管理面）或功能权限不够（按钮级），文案后端都给，
 *        这里不区分，只负责把前端快照刷成服务端的真相；
 *  429 → 配额用尽，同理刷快照。 */
function onErrorStatus(status: number | undefined): void {
  if (status === 401) void handleUnauthorized()
  if (status === 403 || status === 429) void refreshPermSnapshot()
}

http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error?.response?.status
    const raw = error?.response?.data
    // blob 响应（文件导出）：错误体也是 blob，先读成文本再抠 detail，
    // 否则前端只能看到 "Request failed with status code 404" 这种天书。
    if (raw instanceof Blob) {
      return raw.text().then((txt) => {
        onErrorStatus(status)
        const detail = pickDetail(txt) || txt || '请求失败'
        console.error('[API]', detail, error?.config?.url)
        return Promise.reject(new Error(detail))
      })
    }
    onErrorStatus(status)
    const detail = pickDetail(raw) || error?.message || '请求失败'
    console.error('[API]', detail, error?.config?.url)
    return Promise.reject(new Error(detail))
  },
)

/** 后端探活 */
export async function checkHealth() {
  const { data } = await http.get('/health')
  return data as {
    status: string
    version: string
    env: string
    llm_configured: boolean
    redis: boolean
    tools_enabled: boolean
  }
}
