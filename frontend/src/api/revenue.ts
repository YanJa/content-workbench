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

export interface SettlementItem {
  account_id: string
  name: string
  revenue_all_fen: number
  settled_revenue_all_fen: number
  penalty_all_fen: number
  settlement_list: any[]
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
