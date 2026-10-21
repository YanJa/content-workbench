/** 收益看板接口（流量主广告数据）。 */
import { http } from './http'

export interface RevenueAccount {
  id: string
  name: string
}

export interface TrendItem {
  date: string
  income_fen: number
  exposure: number
  click: number
}

export interface CompositionItem {
  slot: string
  income_fen: number
  exposure: number
  click: number
}

export interface RevenueSummary {
  income_fen: number
  exposure: number
  click: number
  click_rate: number
  ecpm_fen: number
  rows: number
}

export interface RevenueOverview {
  range: { start: string; end: string }
  account_count: number
  account_ids: string[]
  summary: RevenueSummary
  trend: TrendItem[]
  composition: CompositionItem[]
}

/** 单条结算区间明细（微信 publisher_settlement 原样透传）。

 * sett_status 官方枚举：1=结算中；2、3=已结算；4=付款中。
 * 注意 API **不提供**「已付款」终态——到账以银行卡实际入账为准。
 */
export interface SettlementRecord {
  month: string // "202609"
  order: number // 1=上半月，2=下半月
  zone: string // "2026年9月16日至30日"
  sett_status: number
  settled_revenue: number
  penalty?: number | null
  sett_no: string
  mail_send_cnt?: string
  date?: string
  slot_revenue?: { slot_id: string; slot_settled_revenue: number }[]
}

export interface SettlementItem {
  account_id: string
  name: string
  revenue_all_fen: number
  settled_revenue_all_fen: number
  penalty_all_fen: number
  settlement_list: SettlementRecord[]
}

export interface RevenueSettlement {
  range: { start: string; end: string }
  items: SettlementItem[]
  totals: {
    revenue_all_fen: number
    settled_revenue_all_fen: number
    penalty_all_fen: number
  }
}

export async function revenueAccounts(): Promise<RevenueAccount[]> {
  const { data } = await http.get('/revenue/accounts')
  return data
}

export async function revenueOverview(params: {
  account_id?: string | null
  start: string
  end: string
  refresh?: boolean
}): Promise<RevenueOverview> {
  const { data } = await http.get('/revenue/overview', {
    params: {
      start: params.start,
      end: params.end,
      ...(params.account_id ? { account_id: params.account_id } : {}),
      ...(params.refresh ? { refresh: 1 } : {}),
    },
  })
  return data
}

export async function revenueSettlement(params: {
  account_id?: string | null
  start: string
  end: string
}): Promise<RevenueSettlement> {
  const { data } = await http.get('/revenue/settlement', {
    params: {
      start: params.start,
      end: params.end,
      ...(params.account_id ? { account_id: params.account_id } : {}),
    },
  })
  return data
}

/** 从 content-disposition 里取文件名：优先 RFC 5987 的 filename*（中文），
 *  兜底普通 filename，再兜底 null。 */
function pickExportFilename(cd?: string): string | null {
  if (!cd) return null
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim())
    } catch {
      return star[1].trim()
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(cd)
  return plain ? plain[1].trim() : null
}

/**
 * 导出收益数据（走后端 /revenue/export）。
 *
 * 只导看板同款的已缓存数据（T+1 口径），区间没数据时后端返回 404，
 * 文案会由 http 拦截器从 blob 里解析出来直接抛给调用方。
 * 下载用临时 <a> + object URL 触发，用完即焚。
 */
export async function revenueExport(params: {
  account_id?: string | null
  start: string
  end: string
  format: 'xlsx' | 'csv'
}): Promise<void> {
  const { data, headers } = await http.get('/revenue/export', {
    params: {
      start: params.start,
      end: params.end,
      format: params.format,
      ...(params.account_id ? { account_id: params.account_id } : {}),
    },
    responseType: 'blob',
  })
  const fname =
    pickExportFilename(headers?.['content-disposition']) ||
    `流量主收益_${params.start}_${params.end}.${params.format}`
  const url = URL.createObjectURL(data)
  const a = document.createElement('a')
  a.href = url
  a.download = fname
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
