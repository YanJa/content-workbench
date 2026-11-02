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
        return Promise.reject(toApiError(detail, status))
      })
    }
    onErrorStatus(status)
    const detail = pickDetail(raw) || error?.message || '请求失败'
    console.error('[API]', detail, error?.config?.url)
    return Promise.reject(toApiError(detail, status))
  },
)

/**
 * 请求失败统一转成 Error，但**保留原始 detail 对象**。
 *
 * 为什么不能直接 `new Error(detail)`：detail 可能是对象（例如模型池换
 * embedding 模型时的 409 `{code:'EMBED_DIM_MISMATCH', stale_chunks: 12}`），
 * 塞进 Error 的 message 会变成 "[object Object]"，页面就拿不到
 * stale_chunks 做二次确认了。这里 message 用人类可读文案，原始对象挂在
 * `err.detail` 上给需要细粒度处理的调用方。
 */
/** 422 校验错误里高频字段的中文名（其余字段用原文包「」展示） */
const FIELD_LABELS: Record<string, string> = {
  username: '用户名',
  password: '密码',
  invite_code: '邀请码',
  old_password: '原密码',
  new_password: '新密码',
  email: '邮箱',
}

function fieldLabel(loc: unknown[]): string {
  // loc 形如 ['body', 'password']，取最后一段字符串（跳过数组下标这类数字段）
  for (let i = loc.length - 1; i >= 0; i--) {
    const seg = loc[i]
    if (typeof seg === 'string') return FIELD_LABELS[seg] ?? `「${seg}」`
  }
  return '字段'
}

/** FastAPI 422 的 detail 是 pydantic 错误数组，直接 stringify 会糊一脸天书
 *  （如 string_too_short / min_length=1）。翻成「密码不能为空」这种人话；
 *  只展示前 3 条，多余的说个总数。 */
function humanizeValidation(items: unknown[]): string {
  const parts = items.slice(0, 3).map((raw) => {
    const it = (raw ?? {}) as Record<string, unknown>
    const loc = Array.isArray(it.loc) ? (it.loc as unknown[]) : []
    const label = fieldLabel(loc)
    const type = String(it.type ?? '')
    const ctx = (it.ctx ?? {}) as Record<string, unknown>
    if (type === 'string_too_short') {
      const min = typeof ctx.min_length === 'number' ? ctx.min_length : 0
      return min > 1 ? `${label}至少 ${min} 个字符` : `${label}不能为空`
    }
    if (type === 'string_too_long') return `${label}过长`
    if (type === 'missing') return `缺少${label}`
    return `${label}填写有误`
  })
  const more = items.length > 3 ? ` 等 ${items.length} 处问题` : ''
  return `${parts.join('；')}${more}`
}

function toApiError(detail: unknown, status?: number): Error {
  let msg = '请求失败'
  if (typeof detail === 'string') msg = detail
  else if (Array.isArray(detail)) msg = humanizeValidation(detail)
  else if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>
    msg = typeof d.message === 'string' ? d.message : JSON.stringify(detail)
  }
  const err = new Error(msg) as Error & { detail?: unknown; status?: number }
  if (detail && typeof detail === 'object') err.detail = detail
  if (status) err.status = status
  return err
}

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
