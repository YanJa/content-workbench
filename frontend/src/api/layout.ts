/** 内容加工接口：排版 + 配图。 */
import { http } from './http'
import type { ContentImage, ContentItem } from '@/types'

export interface ThemeOption {
  id: string
  name: string
  color: string
  scenes: string
  desc: string
}

export interface LayoutResult {
  ok: boolean
  theme: string
  theme_name: string
  html_len: number
  errors: string[]
  warnings: string[]
  layout_status: string
  /** 小红书文本规整时返回 */
  text?: string
}

/** 排版任务提交后的返回。 */
export interface LayoutTask {
  ok: boolean
  task_id: string
  status: string
  message?: string
  poll: string
}

/** 排版任务轮询状态。 */
export interface LayoutJobStatus {
  task_id: string
  content_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  progress: number
  stage: string
  result: LayoutResult | null
  error: string
}

export async function getThemeOptions(contentId: string): Promise<{
  themes: ThemeOption[]
  default: string
  gen_ready: boolean
  /** 封面通道是否可用（脚本 + 本机浏览器都在） */
  cover_ready: boolean
  cover_styles: Record<string, string>
  cover_sizes: Record<string, string>
  cover_default_style: string
  cover_default_tag: string
}> {
  const { data } = await http.get(`/content/items/${contentId}/theme-options`)
  return data
}

/** 提交排版任务（**秒回** task_id，真正排版在后台跑，需轮询 getLayoutJob）。
 *  accountId：关注卡用哪个公众号（留空 = 默认号），用于替换 {{号名}}/{{slogan}} 并注入二维码。 */
export async function layoutContent(
  contentId: string,
  theme: string,
  platform: 'gzh' | 'xiaohongshu' | 'zhihu' = 'gzh',
  autoCover = true,
  authorName = '',
  authorIntro = '',
  accountId = '',
  autoTags = true,
): Promise<LayoutTask> {
  // 排版单次约 3 分钟，远超全局 60s 超时 —— 这里单独把本次请求的超时放宽到 30s，
  // 仅用于「提交任务」这一步（正常应是毫秒级返回），避免极端网络下误判。
  const { data } = await http.post(
    `/content/items/${contentId}/layout`,
    {
      theme,
      platform,
      auto_cover: autoCover,
      auto_tags: autoTags,
      author_name: authorName,
      author_intro: authorIntro,
      account_id: accountId,
    },
    { timeout: 30000 },
  )
  return data
}

/** 轮询排版任务进度 / 结果。404 = 任务中断，需重新排版。 */
export async function getLayoutJob(
  contentId: string,
  taskId: string,
): Promise<LayoutJobStatus> {
  const { data } = await http.get(`/content/items/${contentId}/layout/${taskId}`)
  return data
}

/** 取消「加工中」状态（解卡）：清后端 processing 标记并停止前台等待。 */
export async function cancelProcessing(
  contentId: string,
): Promise<{ ok: boolean; cleared: string; task_id: string }> {
  const { data } = await http.post(`/content/items/${contentId}/cancel-processing`)
  return data
}

/** 是否有可用的生图脚本（没有就引导手动上传）。 */
export async function generateImages(
  contentId: string,
  payload: {
    prompt: string
    size?: string
    count?: number
    position?: number
    provider?: string
  },
): Promise<{ ok: boolean; images: ContentImage[] }> {
  const { data } = await http.post(`/content/items/${contentId}/images/generate`, payload)
  return data
}

/** 文内配图可用通道能力位（script / cloudflare）。 */
export async function getImageProviders(): Promise<{ script: boolean; cloudflare: boolean }> {
  const { data } = await http.get(`/content/image-providers`)
  return data
}

/** 封面生成参数（都可省，留空由后端从正文解析 / 用 .env 默认值）。 */
export interface CoverGenPayload {
  title?: string
  sub?: string
  tag?: string
  style?: string
  size?: string
  date?: string
  /** 排版主题 id：让封面配色跟随该主题（留空用默认品牌色） */
  theme?: string
}

/**
 * 生成封面（模板化，与 AI 生图是两条独立通道）。
 * 产出图会插到配图首位，重复调用 = 换封面（旧图自动清理）。
 */
export async function generateCover(
  contentId: string,
  payload: CoverGenPayload,
): Promise<{ ok: boolean; images: ContentImage[] }> {
  const { data } = await http.post(`/content/items/${contentId}/covers/generate`, payload, {
    timeout: 60000,
  })
  return data
}

export async function uploadImage(
  contentId: string,
  file: File,
): Promise<{ ok: boolean; images: ContentImage[] }> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post(`/content/items/${contentId}/images/upload`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export async function updateImage(
  contentId: string,
  imageId: string,
  patch: { alt?: string; position?: number },
): Promise<{ ok: boolean; images: ContentImage[] }> {
  const { data } = await http.put(`/content/items/${contentId}/images/${imageId}`, patch)
  return data
}

export async function deleteImage(
  contentId: string,
  imageId: string,
): Promise<{ ok: boolean; images: ContentImage[] }> {
  const { data } = await http.delete(`/content/items/${contentId}/images/${imageId}`)
  return data
}

/** 重新取单条内容（排版/配图后刷新）。 */
export async function refreshContent(contentId: string): Promise<ContentItem> {
  const { data } = await http.get(`/content/items/${contentId}`)
  return data
}
