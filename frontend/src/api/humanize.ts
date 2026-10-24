/** 人工痕迹体检与「加痕迹」改写。 */
import { http } from './http'

/** 单条命中：是什么毛病、在哪、多严重、怎么改。 */
export interface TraceHit {
  key: string
  label: string
  severity: 'high' | 'mid' | 'low'
  count: number
  samples: string[]
  how: string
}

/** 体检报告。score 是**人工痕迹指数，越高越像人写的**（不是 AI 率）。 */
export interface TraceReport {
  ok: boolean
  score: number
  level: 'natural' | 'slight' | 'obvious' | 'heavy' | 'unknown'
  level_label: string
  platform: string
  stats: Record<string, number>
  hits: TraceHit[]
  tips: string[]
  /** 必须展示给用户的口径说明（短文会说明统计层未参与，正常样本会说明这不是判定） */
  note: string
  short_sample: boolean
  error?: string
}

export interface HumanizeResult {
  ok: boolean
  text: string
  before_score: number
  after_score: number
  /** 改写后丢了数字/链接/专名的告警（事实校验，绝不静默） */
  warnings: string[]
  level: string
  platform: string
  error?: string
}

export interface HumanizeJob {
  task_id: string
  content_id: string
  platform: string
  level: string
  status: 'queued' | 'running' | 'done' | 'error'
  progress: number
  stage: string
  result: HumanizeResult | null
  error: string
}

export interface LevelOption {
  id: string
  label: string
  desc: string
}

/** 体检：零 LLM、零成本，可以随便点。content_id 与 text 至少给一个。 */
export async function scanTrace(payload: {
  content_id?: string
  text?: string
  platform?: string
  /** 子形态（知乎：answer/article/idea；公众号：article/tietu） */
  sub_type?: string
}): Promise<TraceReport> {
  const { data } = await http.post('/humanize/scan', payload)
  return data
}

/** 提交改写任务（会消耗模型额度），秒回 task_id。level 留空 = 按平台推荐。 */
export async function submitHumanize(payload: {
  content_id: string
  platform: string
  level?: string
  /** 子形态（知乎：answer/article/idea；公众号：article/tietu） */
  sub_type?: string
}): Promise<{ ok: boolean; task_id: string; status: string }> {
  const { data } = await http.post('/humanize/rewrite', payload)
  return data
}

export async function getHumanizeJob(taskId: string): Promise<HumanizeJob> {
  const { data } = await http.get(`/humanize/job/${taskId}`)
  return data
}

/** 把改写结果写回源文。排版产物会一并失效，需要重新排版。 */
export async function applyHumanize(contentId: string, text: string): Promise<{
  ok: boolean
  content_id: string
  score: number
  message: string
}> {
  const { data } = await http.post('/humanize/apply', { content_id: contentId, text })
  return data
}

/** 强度档与平台默认值（不写死在前端，改档位不用发版）。 */
export async function getHumanizeLevels(): Promise<{
  levels: LevelOption[]
  platform_default: Record<string, string>
}> {
  const { data } = await http.get('/humanize/levels')
  return data
}

/** 朱雀检测单段：AIGC 值 + 类型（0=人工 1=AI 2=疑似AI）。 */
export interface ZhuqueSegment {
  text: string
  label: number
  label_name: string
  conf: number
  order: number
  position: number[]
}

/** 朱雀 AI 生成检测结果（腾讯云 @makers/zhuque-text，消耗 Makers 免费 token 额度）。 */
export interface ZhuqueResult {
  ok: boolean
  configured?: boolean
  human_ratio: number
  ai_ratio: number
  suspect_ratio: number
  softmax_confidence: number
  ratio_confidence: number
  segments: ZhuqueSegment[]
  tokens_used?: number
  ms?: number
  error?: string
}

/** 朱雀 AI 检测（同步返回，会消耗 Makers 免费 token 额度）。 */
export async function zhuqueDetect(payload: {
  content_id?: string
  text?: string
}): Promise<ZhuqueResult> {
  const { data } = await http.post('/humanize/zhuque', payload)
  return data
}

/** 下载「AI 生成检测报告单」HTML（朱雀 + 本地体检，浏览器打开后可 Ctrl+P 存 PDF）。 */
export async function downloadDetectReport(payload: {
  content_id?: string
  text?: string
  platform?: string
  /** 子形态（知乎：answer/article/idea；公众号：article/tietu） */
  sub_type?: string
}): Promise<Blob> {
  const resp = await http.post('/humanize/report', payload, { responseType: 'blob' })
  return resp.data
}
