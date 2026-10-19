/**
 * 知乎热榜接口。
 *
 * 三条纪律（与后端路由分工一致，改动前先读一遍）：
 *   1. **读数走本地库**：后台每半小时抓一次落库，用户浏览不扣知乎额度；
 *   2. **手动刷新仅管理员**：`POST /refresh` 会真实消耗共享额度池，按钮要按
 *      角色藏好，普通人点了也是 403；
 *   3. **前端不复刻热度算法**：heat_score / climb 全部以后端算好的为准，
 *      这里只做展示 —— 复刻一份迟早两边算出两个数。
 */
import { http } from './http'

/** 一条热榜话题（含后端算好的趋势信号） */
export interface HotTopicItem {
  question_id: string
  title: string
  /** 已剥掉 utm 的知乎问题直链 */
  url: string
  /** 平台白送的 ~200 字摘要，可能为 null */
  summary: string | null
  thumbnail_url: string | null
  /** 榜单名次 1-30 */
  rank: number
  /** 与上一次成功抓取的名次差，正数 = 在往上冲 */
  climb: number
  /** 今天首次进榜 */
  is_new: boolean
  /** 首次进榜距今小时数（越大越是长线议题） */
  staying_hours: number
  /** 今天进榜的采样次数 */
  seen_times: number
  /** 综合分：正在冲 + 名次靠前，默认排序依据 */
  heat_score: number
}

export interface HotListResult {
  ok: boolean
  error?: string
  /** 未配置凭证时后端会带上这个字段（true/false），配了则不出现 */
  configured?: boolean
  interval_minutes?: number
  /** 数据批次时间（unix 秒） */
  fetched_at?: number
  items: HotTopicItem[]
}

/** 拉取热榜。默认按 heat_score（爬升权重高于名次）取前 limit 条。 */
export async function fetchHotList(
  limit = 8,
  sort: 'trend' | 'rank' = 'trend',
): Promise<HotListResult> {
  const { data } = await http.get('/zhihu/hot', { params: { limit, sort } })
  return data
}

export interface RefreshHotResult {
  ok: boolean
  /** 入库条数 */
  saved?: number
  /** 被关键词过滤掉的条数 */
  blocked?: number
  /** 未配置凭证 / 抓取失败的原因 */
  error?: string
}

/**
 * 手动抓一次热榜（仅管理员）。
 *
 * 这是全站唯一会真实消耗知乎额度的用户动作 —— 后台定时任务本身会跑，
 * 这个按钮是给「我等不及下一轮」用的，调用要慎重。
 */
export async function refreshHot(): Promise<RefreshHotResult> {
  const { data } = await http.post('/zhihu/refresh')
  return data
}
