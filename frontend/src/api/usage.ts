/** AI 用量接口。 */
import { http } from './http'

/**
 * 计费性质三态。**别只用「known」去猜**：
 *   - free：免费模型，费用 0 元，但吃的是每日额度（不是零成本）
 *   - paid：有它自己的单价，按价算
 *   - unknown：没登记 / 登记了没填价 → 按兜底价估，界面要标 ⚠
 */
export type BillingKind = 'free' | 'paid' | 'unknown'

export interface UsageStat {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  total_tokens: number
  cost: number
  avg_latency_ms: number
  /** 免费模型的调用次数（费用为 0，但额度在消耗） */
  free_calls?: number
  free_tokens?: number
}

export interface UsageSeriesItem extends UsageStat {
  key: string
  /** 仅 by_model 有：展示名 / 单价是否登记 / 实际按哪个模型的价在算 */
  label?: string
  known?: boolean
  priced_as?: string
  billing?: BillingKind
}

export interface UsageSummary {
  total: UsageStat
  by_day: UsageSeriesItem[]
  by_model: UsageSeriesItem[]
  by_scene: UsageSeriesItem[]
}

export interface UsageRecord extends UsageStat {
  id: string
  conversation_id: string | null
  message_id: string | null
  scene: string
  model: string
  task_type: string
  latency_ms: number
  is_peak: boolean
  is_summary: boolean
  estimated: boolean
  created_at: number
  model_label: string
  price_known: boolean
  /** 这次调用算哪种账。老数据没有这个字段，前端按 price_known 兜底判断 */
  billing?: BillingKind
}

export interface TopConversation extends UsageStat {
  conversation_id: string
  title: string
}

export interface PriceRow {
  model: string
  label: string
  vendor: string
  unit: string
  context_window: number | null
  reasoning: boolean
  /** 该模型有没有缓存命中价（没有就不显示这一列，显示 0 会让人以为缓存免费） */
  has_cache_price: boolean
  idle: { in_cached?: number; in: number; out: number }
  peak: { in_cached?: number; in: number; out: number }
  tiers?: { above: number; in?: number; out?: number }[]
}

export interface PricingState {
  models: number
  default_model: string
  peak_multiplier: number
  peak_windows: string[]
  timezone: string
  file: string | null
  file_loaded: boolean
  env_loaded: boolean
}

export async function usageSummary(params: {
  days?: number
  conversation_id?: string
  start?: number
  end?: number
}): Promise<UsageSummary> {
  const { data } = await http.get('/usage/summary', { params })
  return data
}

export async function usageRecords(params: {
  limit?: number
  offset?: number
  days?: number
  conversation_id?: string
  model?: string
  scene?: string
}): Promise<{ items: UsageRecord[]; total: number; limit: number; offset: number }> {
  const { data } = await http.get('/usage/records', { params })
  return data
}

/**
 * days 传 null 表示「不限时间」——注意不能塞进 params 变成 days=null，
 * axios 会真的把这个值发出去，后端会按 int 校验打回来。
 */
export async function usageTopConversations(
  days: number | null = 30,
  limit = 10,
): Promise<TopConversation[]> {
  const params: Record<string, number> = { limit }
  if (days) params.days = days
  const { data } = await http.get('/usage/top-conversations', { params })
  return data
}

export async function usagePricing(): Promise<{ items: PriceRow[]; state: PricingState }> {
  const { data } = await http.get('/usage/pricing')
  return data
}

/** 改完 config/pricing.json 后热加载，不用重启后端 */
export async function reloadPricing(): Promise<{ items: PriceRow[]; state: PricingState }> {
  const { data } = await http.post('/usage/pricing/reload')
  return data
}

/** 一次调用的费用拆解（聊天页每条回复下方的 ¥ 数字就是它算的） */
export interface UsageBreakdown {
  model: string
  label: string
  known: boolean
  billing?: BillingKind
  is_peak: boolean
  peak_multiplier: number
  parts: { key: string; label: string; tokens: number; unit_price: number; cost: number }[]
  cost: number
}

/** 估算一次调用费用。计价规则留在后端（峰谷/阶梯/别名都在引擎里）。 */
export async function estimateUsage(p: {
  prompt: number
  completion: number
  cached?: number
  model?: string
}): Promise<UsageBreakdown> {
  const { data } = await http.get('/usage/estimate', {
    params: {
      prompt: p.prompt,
      completion: p.completion,
      cached: p.cached ?? 0,
      ...(p.model ? { model: p.model } : {}),
    },
  })
  return data
}
