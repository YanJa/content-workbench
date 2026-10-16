/** 发布队列与内容接口。 */
import { http } from './http'
import type { ContentItem, QueueItem, QueuePage, QueueStats, QueueStatus } from '@/types'

export interface ListQueueParams {
  status?: string
  platform?: string
  page?: number
  pageSize?: number
}

/** 分页拉取发布队列（筛选在后端做）。 */
export async function listQueue(params: ListQueueParams = {}): Promise<QueuePage> {
  const { status = 'all', platform = 'all', page = 1, pageSize = 20 } = params
  const { data } = await http.get('/queue/items', {
    params: { status, platform, page, page_size: pageSize },
  })
  return data
}

export async function queueStats(): Promise<QueueStats> {
  const { data } = await http.get('/queue/stats')
  return data
}

export async function updateQueueStatus(id: string, status: QueueStatus): Promise<QueueItem> {
  const { data } = await http.patch(`/queue/items/${id}`, { status })
  return data
}

export async function updateChecklist(
  id: string,
  checklist: Record<string, boolean>,
): Promise<QueueItem> {
  const { data } = await http.put(`/queue/items/${id}/checklist`, { checklist })
  return data
}

export async function deleteQueueItem(id: string): Promise<void> {
  await http.delete(`/queue/items/${id}`)
}

export async function exportQueue(): Promise<string> {
  const { data } = await http.get('/queue/export')
  return data.markdown
}

export async function listContents(platform = 'all'): Promise<ContentItem[]> {
  const { data } = await http.get('/content/items', { params: { platform } })
  return data
}

/** 取单条内容（队列页推草稿箱时需要正文）。 */
export async function getContent(contentId: string): Promise<ContentItem> {
  const { data } = await http.get(`/content/items/${contentId}`)
  return data
}

/** 部分更新内容（加工排版面板里编辑正文 / 排版 HTML 后保存）。只传要改的字段。 */
export async function updateContent(
  contentId: string,
  patch: { title?: string; body?: string; summary?: string; body_html?: string },
): Promise<ContentItem> {
  const { data } = await http.patch(`/content/items/${contentId}`, patch)
  return data
}

export interface SaveToQueuePayload {
  platform: string
  title: string
  body: string
  summary?: string
  kind?: 'text' | 'video'
  conversation_id?: string | null
  msg_id?: string | null
}

/**
 * 把对话产出的内容保存到发布队列（状态为「待加工」）。
 *
 * 这是唯一的入队入口 —— 对话侧不再让 AI 自动入队，必须用户显式点按钮。
 */
export async function saveToQueue(
  payload: SaveToQueuePayload,
): Promise<{ ok: boolean; content_id: string; queue_item_id: string }> {
  const { data } = await http.post('/content/items', payload)
  return data
}
