/**
 * 微博热搜接口（灵感模块的第二个热点源，方案 A：免费聚合 hotboard）。
 *
 * 后端实时拉取 uapis.cn 的微博热搜，登录即可调用，不消耗知乎额度。
 * 详见后端 app/services/weibo.py 的免责说明（第三方聚合，仅用于选题灵感）。
 */
import { http } from './http'

/** 一条微博热搜（后端已按统一形状整理） */
export interface WeiboHotItem {
  /** 名次 1-N */
  rank: number
  title: string
  /** 微博搜索链接（带 #话题# 的 search 入口） */
  url: string
  /** 热度值（已尽力归一为整数，可能为 0） */
  hot: number
}

export interface WeiboHotResult {
  ok: boolean
  /** 固定 'weibo'，便于前端区分来源 */
  source: string
  items: WeiboHotItem[]
  /** 聚合接口的数据时刻（ISO 8601，UTC） */
  update_time?: string
  error?: string
  /** 后端 60s 内存缓存命中标记（可选） */
  cached?: boolean
}

/** 拉取微博实时热搜榜。 */
export async function fetchWeiboHot(limit = 20): Promise<WeiboHotResult> {
  const { data } = await http.get('/weibo/hot', { params: { limit } })
  return data
}
