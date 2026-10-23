/** 知识库（RAG 素材）接口。 */
import { http } from './http'
import type {
  KnowledgeHit,
  KnowledgeItem,
  KnowledgePage,
  KnowledgeSource,
  KnowledgeStats,
} from '@/types'

export interface ListKnowledgeParams {
  q?: string
  source?: string
  status?: string
  page?: number
  pageSize?: number
}

/** 分页拉取素材（筛选/搜索在后端做）。 */
export async function listKnowledge(params: ListKnowledgeParams = {}): Promise<KnowledgePage> {
  const { q = '', source = 'all', status = 'all', page = 1, pageSize = 20 } = params
  const { data } = await http.get('/knowledge/items', {
    params: { q, source, status, page, page_size: pageSize },
  })
  return data
}

export async function knowledgeStats(): Promise<KnowledgeStats> {
  const { data } = await http.get('/knowledge/stats')
  return data
}

export interface CreateKnowledgePayload {
  title?: string
  /** 直接给原文（手动录入） */
  text?: string
  /** 复用已产成稿（后端取 Content.body） */
  content_id?: string
  origin_ref?: string
}

export interface CreateKnowledgeResult {
  item: KnowledgeItem
  already_exists: boolean
}

export async function createKnowledge(payload: CreateKnowledgePayload): Promise<CreateKnowledgeResult> {
  const { data } = await http.post('/knowledge/items', payload)
  return { item: data.item, already_exists: Boolean(data.already_exists) }
}

/** 批量查哪些成稿已存进知识库：content_id -> 素材 id（未存的不出现在 map 里）。 */
export async function originCheck(contentIds: string[]): Promise<Record<string, string>> {
  if (!contentIds.length) return {}
  const { data } = await http.get('/knowledge/origin-check', {
    params: { content_ids: contentIds },
  })
  return data ?? {}
}

/** 素材详情（含正文与切分结果）。列表接口不带这些，避免列表页背正文。 */
export async function getKnowledge(id: string): Promise<KnowledgeItem> {
  const { data } = await http.get(`/knowledge/items/${id}`)
  return data
}

export async function renameKnowledge(id: string, title: string): Promise<KnowledgeItem> {
  const { data } = await http.patch(`/knowledge/items/${id}`, { title })
  return data.item
}

export async function deleteKnowledge(id: string): Promise<void> {
  await http.delete(`/knowledge/items/${id}`)
}

/** 重跑失败/中断的向量化。 */
export async function retryKnowledge(id: string): Promise<KnowledgeItem> {
  const { data } = await http.post(`/knowledge/items/${id}/retry`)
  return data.item
}

/** 上传文档入库（PDF / TXT / Word / JSON / Markdown）。返回 {item, already_exists}。 */
export async function uploadKnowledge(
  file: File,
  title?: string,
): Promise<CreateKnowledgeResult> {
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  // 不手动设 Content-Type：axios 对 FormData 会自动带上 multipart 边界
  const { data } = await http.post('/knowledge/upload', form)
  return { item: data.item, already_exists: Boolean(data.already_exists) }
}

export interface ReindexResult {
  ok: boolean
  /** 重新排队的片段数 */
  queued: number
  /** 涉及的素材条数 */
  items: number
  model: string
}

/**
 * 重建「模型过期」的向量索引。
 *
 * 换过 embedding 模型（在模型池里改了「知识库向量化」槽）之后必须跑一次：
 * 旧向量还在库里、状态还是 ready，但它们和当前模型算出的 query 向量不在
 * 同一个空间，检索会**静默**漏掉全部旧素材 —— 不报错，只是搜不到。
 */
export async function reindexKnowledge(): Promise<ReindexResult> {
  const { data } = await http.post('/knowledge/reembed-all')
  return data
}

/** 召回调试：看一句话能命中哪些素材（与写稿时走同一条检索）。 */
export async function searchKnowledge(
  query: string,
  topK = 5,
): Promise<KnowledgeHit[]> {
  const { data } = await http.post('/knowledge/search', { query, top_k: topK })
  return data.hits ?? []
}

export const KNOWLEDGE_SOURCE_LABEL: Record<KnowledgeSource, string> = {
  manual: '手动录入',
  content: '来自成稿',
  upload: '文档上传',
  url: '网页抓取',
}
