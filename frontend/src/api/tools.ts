/** 工具与视频接口。 */
import { http } from './http'
import type { RenderJob, ToolInfo } from '@/types'

export async function listTools(): Promise<ToolInfo[]> {
  const { data } = await http.get('/tools')
  return data
}

/** 用户确认后执行危险工具。
 *  `conversation_id` 可选：对话页确认时传真实会话 id（结果回写消息流）；
 *  队列页 / 加工面板的推送不来自任何对话，不传即可——**不要塞占位字符串**，
 *  否则后端会把它当真实会话查而报 404「会话不存在」。 */
export async function confirmTool(payload: {
  conversation_id?: string | null
  tool_call_id: string
  tool_name: string
  arguments: Record<string, unknown>
}) {
  const { data } = await http.post('/chat/confirm', payload)
  return data as { ok: boolean; result: Record<string, unknown> }
}

export interface VisualScene {
  narration: string
  title: string
  body: string[]
  /** 真实幻灯片图路径（LibreOffice 渲染），空串表示退回文字卡片 */
  slide_image?: string
}

export async function submitRender(payload: {
  script: string
  ratio?: string
  voice?: string
  subtitle?: boolean
  title?: string
  visual_scenes?: VisualScene[]
}) {
  const { data } = await http.post('/video/render', payload)
  return data as { ok: boolean; job_id: string; message: string }
}

export async function getRenderJob(jobId: string): Promise<RenderJob> {
  const { data } = await http.get(`/video/jobs/${jobId}`)
  return data
}

/**
 * 轮询专用的精简进度（约 200 字节，完整详情约 22KB）。
 *
 * 刻意用 Partial<RenderJob> 取子集，而不是另起一个 interface：
 * 合并回 job.value 时类型天然兼容，也保证字段不会和 RenderJob 漂移。
 */
export type JobProgress = Pick<
  RenderJob,
  | 'id'
  | 'status'
  | 'progress'
  | 'stage'
  | 'error'
  | 'output_url'
  | 'output_path'
  | 'duration_sec'
  | 'size_bytes'
  | 'finished_at'
>

/**
 * 只取进度，不带 script / visual_scenes。
 *
 * 完整接口经 Cloudflare Tunnel 单次要 1.7~2.9 秒，其中十几 KB 是轮询用不到的
 * 大字段。轮询走这个接口把响应压到 200 字节左右；任务结束时再调一次
 * getRenderJob 拿完整数据回填编辑区。
 */
export async function getRenderProgress(jobId: string): Promise<JobProgress> {
  const { data } = await http.get(`/video/jobs/${jobId}/progress`)
  return data
}

/** 任务列表（后端持久化，刷新页面历史不丢） */
export interface JobPage {
  items: RenderJob[]
  total: number
  page: number
  pageSize: number
  pages: number
}

/** 任务列表分页查询（1 基页码；status/keyword 可选过滤） */
export async function listRenderJobs(
  page = 1,
  pageSize = 10,
  status?: string,
  keyword?: string,
): Promise<JobPage> {
  const params: Record<string, string | number> = { page, pageSize }
  if (status && status !== 'all') params.status = status
  if (keyword?.trim()) params.keyword = keyword.trim()
  const { data } = await http.get('/video/jobs', { params })
  return {
    items: data.items,
    total: data.total,
    page: data.page,
    pageSize: data.page_size,
    pages: data.pages,
  }
}

/** 失败的任务沿原参数重跑 */
export async function retryRenderJob(jobId: string): Promise<RenderJob> {
  const { data } = await http.post(`/video/jobs/${jobId}/retry`)
  return data
}

/** 改名 / 改口播稿 / 改每页结构 / 换音色（渲染中的任务会 409） */
export async function updateRenderJob(
  jobId: string,
  patch: { title?: string; script?: string; visual_scenes?: VisualScene[]; voice?: string },
): Promise<RenderJob> {
  const { data } = await http.patch(`/video/jobs/${jobId}`, patch)
  return data
}

/* ---------------------------------------------------------------- 声音复刻 */

/** 复刻后端状态：provider 决定「录音克隆」能不能用 */
export interface VoiceProviderStatus {
  provider: 'edge' | 'cloud' | 'xtts'
  xtts_installed: boolean
  cloud_ready: boolean
  available: boolean
  note: string
}

/** 已复刻的音色 */
export interface CloneVoice {
  id: string
  /** 直接可用的音色值，形如 clone_ab12cd34ef56 */
  value: string
  name: string
  provider: string
  duration: number
  original_duration?: number
  sample_url: string
  created_at: number
  quality?: { level: 'ok' | 'warn' | 'bad'; message: string }
  fingerprint?: { rms?: number; peak?: number; zcr?: number }
}

/** 复刻任务进度 */
export interface CloneJob {
  profile_id: string
  status: 'queued' | 'running' | 'done' | 'failed'
  progress: number
  stage: string
  error?: string
  quality?: { level: string; message: string }
}

/** 当前复刻后端能力 */
export async function getVoiceProvider(): Promise<VoiceProviderStatus> {
  const { data } = await http.get('/video/voices/provider')
  return data
}

/** 已复刻音色列表（含试听地址）
 *  注意路径是 /video/voices/clones：/video/voices 是官方音色清单（tools 路由），
 *  两者同路径会因注册顺序冲突，复刻列表永远拿不到。 */
export async function listCloneVoices(): Promise<{
  items: CloneVoice[]
  provider: VoiceProviderStatus
}> {
  const { data } = await http.get('/video/voices/clones')
  return data
}

/** 上传录音，后台开始复刻（返回后轮询 getCloneStatus） */
export async function cloneVoice(file: File, name: string): Promise<{
  ok: boolean
  id: string
  name: string
  message: string
}> {
  const form = new FormData()
  form.append('file', file)
  form.append('name', name)
  const { data } = await http.post('/video/voices/clone', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  })
  return data
}

/** 复刻进度 */
export async function getCloneStatus(profileId: string): Promise<CloneJob> {
  const { data } = await http.get(`/video/voices/clone/${profileId}`)
  return data as CloneJob
}

export async function renameCloneVoice(profileId: string, name: string): Promise<CloneVoice> {
  const { data } = await http.put(`/video/voices/${profileId}`, { name })
  return data
}

export async function deleteCloneVoice(profileId: string): Promise<void> {
  await http.delete(`/video/voices/${profileId}`)
}

export async function deleteRenderJob(jobId: string): Promise<void> {
  await http.delete(`/video/jobs/${jobId}`)
}

export interface PptParseResult {
  title: string
  slides_count: number
  script: string
  chars: number
  estimate_seconds: number
  slides: { index: number; text: string; notes: string; title: string; points: string[] }[]
  /** 每页标题+要点，随渲染提交，让画面展示幻灯片内容而不是口播句 */
  visual_scenes: VisualScene[]
  /** 成功渲染成图片的页数（LibreOffice），少于 slides_count 时部分页退回文字卡片 */
  slide_images_ok?: number
}

/** 上传 pptx 解析出口播稿（不渲染，先给用户看文本） */
export async function parsePpt(file: File): Promise<PptParseResult> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post('/video/ppt/parse', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000,
  })
  return data
}
