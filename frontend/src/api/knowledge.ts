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

export async function createKnowledge(payload: CreateKnowledgePayload): Promise<KnowledgeItem> {
  const { data } = await http.post('/knowledge/items', payload)
  return data.item
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
