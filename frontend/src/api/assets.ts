/**
 * 素材库接口。
 *
 * 一个使用约定：`url` / `thumb_url` 都是**后端现签的短时直链**，
 * 不要缓存到本地状态里长期持有（签名过期就裂图），每次拉列表重新取。
 */
import { http } from './http'

/** 一条已入库素材 */
export interface Asset {
  id: string
  kind: 'image' | 'video'
  /** upload / search / ai / cover */
  source: string
  /** pixabay / openverse / coverr / 空（自有） */
  source_name: string
  source_url: string
  /** pixabay-content / cc0 / cc-by / own / unknown */
  license: string
  license_url: string
  author: string
  author_url: string
  /** 现成的署名行，用到文章里可直接附上 */
  attribution_text: string
  title: string
  tags: string[]
  width: number
  height: number
  duration: number
  size_bytes: number
  mime: string
  watermark_state: string
  used_count: number
  created_at: number
  url: string
  thumb_url: string
}

/** 在线搜索候选（还没入库，所以没有 id 与本地 url） */
export interface AssetCandidate {
  source_name: string
  source_id: string
  title: string
  thumb_url: string
  full_url: string
  width: number
  height: number
  author: string
  author_url: string
  license: string
  license_url: string
  attribution_text: string
  source_url: string
  tags: string[]
  mime: string
  /** Openverse 的中文许可说明（仅该源有） */
  license_label?: string
}

export interface AssetSourceStatus {
  name: string
  label: string
  configured: boolean
  note: string
  hint: string
}

export interface AssetStats {
  total: number
  image_count: number
  video_count: number
  used_bytes: number
  quota_bytes: number
  by_source: { source: string; count: number }[]
}

export interface ListParams {
  kind?: string
  source?: string
  q?: string
  tag?: string
  page?: number
  page_size?: number
}

export async function listAssets(
  params: ListParams = {},
): Promise<{ ok: boolean; items: Asset[]; total: number; page: number; page_size: number }> {
  const { data } = await http.get('/assets', { params })
  return data
}

export async function assetStats(): Promise<AssetStats & { ok: boolean }> {
  const { data } = await http.get('/assets/stats')
  return data
}

export async function uploadAsset(
  file: File,
  title?: string,
): Promise<{ ok: boolean; created: boolean; asset: Asset }> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post('/assets/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    params: title ? { title } : undefined,
    timeout: 60000,
  })
  return data
}

export async function updateAsset(
  id: string,
  patch: { title?: string; tags?: string[] },
): Promise<{ ok: boolean; asset: Asset }> {
  const { data } = await http.patch(`/assets/${id}`, patch)
  return data
}

export async function deleteAsset(id: string): Promise<{ ok: boolean; referenced: number }> {
  const { data } = await http.delete(`/assets/${id}`)
  return data
}

export async function listAssetSources(): Promise<{ ok: boolean; sources: AssetSourceStatus[] }> {
  const { data } = await http.get('/assets/sources')
  return data
}

export async function searchOnline(payload: {
  q: string
  source?: string
  page?: number
  per_page?: number
}): Promise<{
  ok: boolean
  items: AssetCandidate[]
  errors: { source: string; reason: string }[]
  sources: AssetSourceStatus[]
}> {
  const { data } = await http.post('/assets/search', payload, { timeout: 45000 })
  return data
}

/** 导入指定候选。只传来源与源站 ID —— 下载地址由后端回源站查，前端不参与。 */
export async function importCandidate(
  sourceName: string,
  sourceId: string,
): Promise<{ ok: boolean; created: boolean; asset: Asset }> {
  const { data } = await http.post(
    '/assets/import',
    { source_name: sourceName, source_id: sourceId },
    { timeout: 90000 },
  )
  return data
}

/** 把素材插进某篇内容的配图列表（复用现有 Content.images 结构） */
export async function attachAssetToContent(
  contentId: string,
  assetId: string,
): Promise<{ ok: boolean; images: unknown[] }> {
  const { data } = await http.post(`/content/items/${contentId}/images/from-asset`, {
    asset_id: assetId,
  })
  return data
}
