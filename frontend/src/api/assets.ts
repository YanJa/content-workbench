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
  /** 所属目录 id，空串 = 未归类 */
  folder_id: string
  url: string
  thumb_url: string
}

/** 一个素材目录（可多层嵌套，前端用 parent_id 组树） */
export interface AssetFolder {
  id: string
  name: string
  /** 空串 = 根级 */
  parent_id: string
  created_at: number
  /** 本目录 + 全部子目录的素材总数 */
  asset_count?: number
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
  /** 目录过滤：'none' = 未归类；具体 id = 该目录；不传 = 全部 */
  folder_id?: string
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
  folderId?: string | null,
): Promise<{ ok: boolean; created: boolean; asset: Asset }> {
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  if (folderId) form.append('folder_id', folderId)
  const { data } = await http.post('/assets/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  })
  return data
}

/** 批量上传：一次请求带多个文件。单个失败不中断整批，failed 里逐条给原因。 */
export async function uploadAssetBatch(
  files: File[],
  folderId?: string | null,
): Promise<{ ok: boolean; items: Asset[]; failed: { name: string; reason: string }[] }> {
  const form = new FormData()
  for (const f of files) form.append('files', f)
  if (folderId) form.append('folder_id', folderId)
  const { data } = await http.post('/assets/upload-batch', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  })
  return data
}

/** 批量移动素材。folderId 为 null/空 = 移到「未归类」。 */
export async function moveAssets(
  assetIds: string[],
  folderId: string | null,
): Promise<{ ok: boolean; moved: number }> {
  const { data } = await http.post('/assets/move', {
    asset_ids: assetIds,
    folder_id: folderId || null,
  })
  return data
}

/**
 * 打包下载选中素材（zip）。走 blob 下载范式：临时 <a> + object URL，用完即焚。
 * 文件名优先取 content-disposition（后端 RFC5987 签好），取不到就用兜底名。
 */
export async function downloadAssetsZip(assetIds: string[]): Promise<void> {
  const { data, headers } = await http.post(
    '/assets/download',
    { asset_ids: assetIds },
    { responseType: 'blob', timeout: 120000 },
  )
  const cd = headers?.['content-disposition'] as string | undefined
  const star = cd && /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const fname = (star && decodeURIComponent(star[1])) || `素材_${assetIds.length}张.zip`
  const url = URL.createObjectURL(data)
  const a = document.createElement('a')
  a.href = url
  a.download = fname
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export async function updateAsset(
  id: string,
  patch: { title?: string; tags?: string[]; folder_id?: string | null; folder_set?: boolean },
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

// ---------------------------------------------------------------- 按文章采集（受控爬虫）
/**
 * 和「在线搜图」是两套东西，别混：
 *  - 在线搜图 = 查 Pixabay/Openverse 的 API，拿到的是**已明确授权可商用**的素材；
 *  - 按文章采集 = 从你指定的站点页面里抽配图，授权状态未知，入库时
 *    `license=unknown`，卡片上会一直挂着「授权待确认」。
 *
 * 流程同样是两段式：preview 只出候选（不落库），import 勾选后才真正入库。
 */
export interface CrawlConfig {
  ok: boolean
  /** 采集通道整体可用（显式 URL 模式始终可用） */
  enabled: boolean
  /** 能否「按文章自动找配图」——需要后端配了域名白名单 */
  auto_search: boolean
  domains: string[]
  respect_robots: boolean
  use_browser: boolean
  max_pages: number
  max_images: number
  hint: string
}

/** 预览候选。thumb_url 是后端现签的直链，只在 30 分钟内有效。 */
export interface CrawlCandidate {
  id: string
  thumb_url: string
  source_url: string
  page_url: string
  page_title: string
  domain: string
  alt: string
  width: number
  height: number
  size_bytes: number
  mime: string
  fetched_at: number
  /** 采集来源一律 unknown，前端打「授权待确认」 */
  license: string
}

export interface CrawlPreviewResult {
  ok: boolean
  items: CrawlCandidate[]
  errors: { source: string; reason: string }[]
  /** 后端实际用的检索词（没传关键词时由标题/正文提取） */
  keywords: string[]
  pages_visited: number
}

export interface CrawlImportResult {
  ok: boolean
  imported: Asset[]
  imported_count: number
  /** 真新建条数；duplicates 是 hash 去重命中的条数 */
  created: number
  duplicates: number
  failed: { id: string; reason: string }[]
}

export async function crawlConfig(): Promise<CrawlConfig> {
  const { data } = await http.get('/assets/crawl/config')
  return data
}

export async function crawlPreview(payload: {
  title?: string
  body?: string
  keywords?: string[]
  page_urls?: string[]
  max_pages?: number
  max_images?: number
}): Promise<CrawlPreviewResult> {
  // 采集要逐页抓取 + 限速，比普通接口慢得多，超时给足
  const { data } = await http.post('/assets/crawl/preview', payload, { timeout: 180000 })
  return data
}

/** rightsConfirmed 是后端强制的合规闸门，不勾会直接 400。 */
export async function crawlImport(
  ids: string[],
  rightsConfirmed: boolean,
): Promise<CrawlImportResult> {
  const { data } = await http.post(
    '/assets/crawl/import',
    { ids, rights_confirmed: rightsConfirmed },
    { timeout: 120000 },
  )
  return data
}

// ---------------------------------------------------------------- 目录（folder）
/** 当前用户全部目录（扁平）+ 未归类计数。前端自行组树。 */
export async function listAssetFolders(): Promise<{
  ok: boolean
  items: AssetFolder[]
  uncategorized_count: number
}> {
  const { data } = await http.get('/assets/folders')
  return data
}

export async function createAssetFolder(
  name: string,
  parentId?: string | null,
): Promise<{ ok: boolean; folder: AssetFolder }> {
  const { data } = await http.post('/assets/folders', {
    name,
    parent_id: parentId || null,
  })
  return data
}

/** 改名 / 移动。parent_id 为 null 且 folder_set=true 表示移到根级。 */
export async function updateAssetFolder(
  id: string,
  patch: { name?: string; parent_id?: string | null; folder_set?: boolean },
): Promise<{ ok: boolean; folder: AssetFolder }> {
  const { data } = await http.patch(`/assets/folders/${id}`, patch)
  return data
}

/**
 * 删除目录。
 * strategy：'move-to-parent' 素材与子目录挂回父级；'delete-assets' 连同素材全部删除。
 */
export async function deleteAssetFolder(
  id: string,
  strategy: 'move-to-parent' | 'delete-assets',
): Promise<{ ok: boolean; deleted_assets: number; moved_assets: boolean }> {
  const { data } = await http.delete(`/assets/folders/${id}`, { params: { strategy } })
  return data
}

/** 树形视图数据：文件夹树 + 每节点素材 + 未归类素材，一次取齐。 */
export interface AssetTreeNode extends AssetFolder {
  assets: Asset[]
  children: AssetTreeNode[]
}

export async function assetTree(): Promise<{
  ok: boolean
  folders: AssetTreeNode[]
  uncategorized: Asset[]
  uncategorized_count: number
}> {
  const { data } = await http.get('/assets/tree')
  return data
}
