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
  /** 这次**实际**检的是哪一份：layout=排版产物，body=源文。请求 layout 但还没排版时会回落成 body，前端必须据此提示 */
  source?: TraceSource
  error?: string
}

/** 检测对象：layout=排版产物（待发布的那一份，默认），body=源文 Markdown（改写前后的基准） */
export type TraceSource = 'layout' | 'body'

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
  /** 当前阶段百分比不可信（模型调用中）：该展示已用时长而不是死盯进度条 */
  indeterminate?: boolean
  /** 服务端算出的已用秒数（含排队） */
  elapsed?: number
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
  /** 检哪一份：不传 = 排版产物 */
  source?: TraceSource
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
  /** 这次实际检的是哪一份（后端回传，可能因未排版而回落成 body） */
  source?: TraceSource
  error?: string
}

/** 朱雀 AI 检测（同步返回，会消耗 Makers 免费 token 额度）。 */
export async function zhuqueDetect(payload: {
  content_id?: string
  text?: string
  platform?: string
  /** 跟本地体检同一个开关：检排版产物还是源文 */
  source?: TraceSource
}): Promise<ZhuqueResult> {
  const { data } = await http.post('/humanize/zhuque', payload)
  return data
}

/** 本月朱雀 token 用量估算（非权威，详见 note，以 EdgeOne 控制台为准）。 */
export interface ZhuqueUsage {
  /** 月份，'YYYY-MM' */
  month: string
  /** 本月累计消耗 token */
  tokens: number
  /** 本月累计检测次数 */
  calls: number
  /** 月度额度（configured 时才有意义） */
  quota: number
  /** 剩余估算 = max(0, quota - tokens) */
  remaining_est: number
  /** 朱雀密钥是否已配置 */
  configured: boolean
  /** 免责说明：仅本应用内估算，非权威 */
  note: string
}

/** 本月朱雀 token 用量估算（非权威）。 */
export async function getZhuqueUsage(): Promise<ZhuqueUsage> {
  const { data } = await http.get('/humanize/zhuque-usage')
  return data
}

/** 把一次检测结果存成快照，挂到内容的 meta['aigc_trace']（已发布态回看用）。 */
export async function saveTraceSnapshot(
  contentId: string,
  payload: {
    platform?: string
    sub_type?: string
    source?: TraceSource
    scan?: TraceReport | null
    zhuque?: ZhuqueResult | null
  },
): Promise<void> {
  await http.put(`/content/items/${contentId}/trace-snapshot`, payload)
}

/** 单个检测对象（layout/body）的快照槽：体检与朱雀各存各的，互不打断。 */
export interface TraceSnapshotSlot {
  source?: TraceSource
  scan?: TraceReport | null
  zhuque?: ZhuqueResult | null
}

/** 落库的检测快照（Content.meta['aigc_trace']），按检测对象各存一份。 */
export interface TraceSnapshot {
  saved_at?: number
  platform?: string
  sub_type?: string
  layout?: TraceSnapshotSlot
  body?: TraceSnapshotSlot
}

/** 读回已落库的检测快照（重开抽屉恢复朱雀结果用，不重新检测、不花钱）。 */
export async function getTraceSnapshot(contentId: string): Promise<TraceSnapshot | null> {
  const { data } = await http.get(`/content/items/${contentId}`)
  return (data?.meta?.aigc_trace as TraceSnapshot) || null
}

/** 下载「AI 生成检测报告单」HTML（朱雀 + 本地体检，浏览器打开后可 Ctrl+P 存 PDF）。 */
export async function downloadDetectReport(payload: {
  content_id?: string
  text?: string
  platform?: string
  /** 子形态（知乎：answer/article/idea；公众号：article/tietu） */
  sub_type?: string
  source?: TraceSource
}): Promise<Blob> {
  const resp = await http.post('/humanize/report', payload, { responseType: 'blob' })
  return resp.data
}
