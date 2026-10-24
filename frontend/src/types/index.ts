/**
 * 全局类型定义。
 * 与后端 app/schemas/models.py 一一对应，改后端字段时记得同步这里。
 */

export type Platform = 'gzh' | 'xiaohongshu' | 'douyin' | 'zhihu'

/**
 * 知乎子形态。一个知乎平台上有三种完全不同的东西，篇幅差一个数量级
 * （回答 600-2000 / 文章 1500-6000 / 想法 50-300），所以必须显式选。
 * 其余平台恒为空串。
 */
export type ZhihuSubType = 'answer' | 'article' | 'idea'

export type QueueStatus = 'draft' | 'ready' | 'published' | 'archived'

export type JobStatus = 'queued' | 'running' | 'done' | 'failed'

/** 对话会话 */
export interface Conversation {
  id: string
  title: string
  scene: string
  summary: string
  created_at: number
  updated_at: number
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  tool_name?: string | null
  created_at: number
  /** token 用量：历史会话回显当条回复的用量/费用行 */
  prompt_tokens?: number
  completion_tokens?: number
  /** 推理模型的思维链（历史回显「思考过程」折叠块） */
  reasoning?: string
  /** 模型调用失败的错误信息（该轮回复挂的红色提示；刷新/重进会话仍在） */
  error?: string
  /** 展示型工具痕迹：{calls: [{id, name, arguments, result, status}]} */
  tool_calls?: Record<string, unknown> | null
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

/** 对话请求 */
export interface ChatRequest {
  conversation_id?: string | null
  message: string
  platform?: Platform | null
  /** 知乎子形态（仅 platform=zhihu 时有效；后端对非法值回落 answer） */
  sub_type?: string
  /** 知乎回答归属的问题（URL 或 ID），后端据此注入「已有答案边界」 */
  zhihu_question?: string
  topic?: string
  audience?: string
  enable_tools?: boolean
  /** 前端为这条回复预生成的 id，落库时当主键（让 meta.msg_id 与历史消息 id 一致） */
  assistant_msg_id?: string | null
  /**
   * 本轮是否从私有知识库召回素材。默认开（后端 null 视为 true），
   * 关掉后这一轮完全不查素材——临时想让 AI 自由发挥时用。
   */
  use_knowledge?: boolean | null
  /**
   * 本轮是否挂载技能包。默认开（后端 null 视为 true）。
   * 关掉后这一轮不注入技能清单、也不提供 activate_skill 工具。
   */
  use_skills?: boolean | null
}

/**
 * SSE 事件流。
 * 事件类型与后端 app/services/agent.py 保持一致。
 */
export type SSEEvent =
  | { type: 'conversation'; conversation_id: string }
  | { type: 'reasoning'; content: string }
  | { type: 'delta'; content: string }
  | { type: 'tool_call'; id: string; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; id: string; name: string; result: Record<string, unknown> }
  | {
      type: 'tool_confirm'
      id: string
      name: string
      arguments: Record<string, unknown>
      message: string
    }
  | {
      type: 'usage'
      usage: {
        prompt_tokens: number
        completion_tokens: number
        total_tokens: number
        reasoning_tokens?: number
      }
      /** 真正生成这条回复的模型（可能已被自动切换过）。算费用必须用它，
       *  否则每条回复都按默认模型估价 —— 免费模型会被算成花钱。 */
      model?: string
    }
  | { type: 'done'; finish_reason: string }
  | { type: 'error'; message: string }
  /** 模型额度预警（80% / 100%）。后端 model_pool.take_warnings 产出 */
  | { type: 'quota_warning'; level: 'warn' | 'exhausted'; feature: string; model: string; label: string; ratio: number; message: string }
  /** 开流阶段失败 → 已自动切到备选模型（已产出正文则不切换） */
  | { type: 'model_switched'; from: string; to: string; message: string }

/** 工具定义 */
export interface ToolInfo {
  name: string
  description: string
  requires_confirm: boolean
}

/** 内容成品 */
export interface ContentImage {
  id: string
  /** 服务器上的相对路径（用于删除/排序） */
  path: string
  /** 可访问的 URL（/api/content/images/...），用于 <img src> */
  url: string
  alt: string
  source: 'gen' | 'upload' | 'cover'
  prompt?: string
  /** 正文插入位置：0=文首，-1=文末，n=第 n 段后 */
  position: number
}

export interface ContentItem {
  id: string
  platform: Platform
  kind: string
  /** 子形态（知乎：answer / article / idea；公众号：article / tietu；其余平台空串） */
  sub_type?: string
  title: string
  /** 纯内容正文：结构化 Markdown（对话产出的原始内容） */
  body: string
  summary: string
  meta?: Record<string, unknown> | null
  conversation_id?: string | null
  /** 排版后的内联样式 HTML（公众号兼容的 <section> 片段） */
  body_html: string
  theme?: string | null
  layouts?: Record<string, unknown> | null
  images?: ContentImage[] | null
  /** none=未排版 / ready=已排版可发布 */
  layout_status: string
  /** 加工中标记：'' 空闲 / 'layout' 排版中 / 'image' 生图中 */
  processing?: string
  created_at: number
  updated_at: number
}

/** 队列项 */
export interface QueueItem {
  id: string
  content_id: string
  platform: Platform
  title: string
  status: QueueStatus
  checklist?: Record<string, boolean> | null
  wechat_media_id?: string | null
  /** 关联内容的加工中标记，队列行据此显示「排版中/生图中」 */
  processing?: string
  /** 来源会话与消息（从 Content join 出来）：队列行据此「回到对话」并定位 */
  conversation_id?: string | null
  msg_id?: string | null
  created_at: number
  published_at?: number | null
  /** 已被存进知识库时，这里是知识库素材 id（队列行据此显示「已存入 / 查看」） */
  kbStored?: string
  /** 标题现在还能改吗（未排版、没推过草稿箱、没在加工中）。由后端判定，前端不自己推断 */
  title_editable?: boolean
  /** 关联内容的排版状态：none=还没排版 */
  layout_status?: string
  /** 子形态（知乎：answer / article / idea；公众号：article / tietu） */
  sub_type?: string
  /**
   * 「去发布」深链。知乎官方没有发布接口，系统是「生成 → 人带走 → 人发布」，
   * 这里给出对应形态的网页入口。公众号走草稿箱推送，因此为空。
   */
  publish_url?: string
}

/** 微信公众号账号 */
export interface WeChatAccount {
  id: string
  name: string
  appid: string
  /** 后端只回传「有没有配 secret」，不回传明文 */
  secret_set: boolean
  homepage: string
  /** 对外公众号名，用于替换排版产出的 {{号名}} 占位符 */
  gzh_name: string
  /** 关注卡副标，用于替换 {{slogan}} 占位符 */
  slogan: string
  /** 二维码直链（带 owner 参数），可直接用于 <img src>；未配时为空串 */
  qrcode_url: string
  /** 是否已配二维码 */
  qrcode_set: boolean
  is_default: boolean
  /** 上次连通性验证成功时间戳；null = 未验证过 */
  verified_at?: number | null
  created_at: number
  updated_at: number
}

export interface QueueStats {
  total: number
  by_status: Record<string, number>
  by_platform: Record<string, number>
}

/** 分页返回的发布队列。 */
export interface QueuePage {
  items: QueueItem[]
  total: number
  page: number
  page_size: number
}

// ---------------------------------------------------------------- 知识库
/** 素材来源 */
export type KnowledgeSource = 'manual' | 'content' | 'upload' | 'url'
/** 素材状态：pending=排队 / processing=向量化中 / ready=可用 / partial=部分片段失败 / failed=失败 */
export type KnowledgeStatus = 'pending' | 'processing' | 'ready' | 'partial' | 'failed'

export interface KnowledgeItem {
  id: string
  source: KnowledgeSource
  title: string
  origin_ref: string
  status: KnowledgeStatus
  chunk_count: number
  error: string
  char_count: number
  created_at: number
  updated_at: number
  /** 仅详情接口返回 */
  raw_text?: string
  chunks?: KnowledgeChunk[]
}

export interface KnowledgeChunk {
  id: string
  seq: number
  content: string
  char_len: number
  status: KnowledgeStatus
  error: string
}

export interface KnowledgePage {
  items: KnowledgeItem[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface KnowledgeStats {
  items: number
  chunks: number
  ready_chunks: number
  pending_chunks: number
  items_limit: number
  chunks_limit: number
  /** 向量化通道是否已配置（没配则检索退化为关键词匹配） */
  vector_ready: boolean
  embed_model: string
  /**
   * 「已就绪但向量不属于当前模型」的片段数。
   * 非 0 = 换过 embedding 模型，这些片段检索不到（不报错，只是漏召回）。
   */
  stale_chunks: number
}

export interface KnowledgeHit {
  item_id: string
  title: string
  content: string
  score: number
  match: 'vector' | 'keyword'
}

/** 渲染任务 */
export interface RenderJob {
  id: string
  status: JobStatus
  progress: number
  stage: string
  output_path: string
  error: string
  title: string
  source: 'text' | 'ppt'
  ratio: string
  voice: string
  /** 成片可访问 URL（后端已拼好 /api/video/outputs/…） */
  output_url: string
  duration_sec: number
  width: number
  height: number
  size_bytes: number
  created_at: number
  finished_at: number | null
  /** 口播稿与 PPT 每页结构：点击历史任务时回显到编辑区 */
  script: string
  visual_scenes?: { narration: string; title: string; body: string[] }[] | null
}

/** 单条消息的费用估算（ChatView 每条回复下方展示） */
export interface MessageUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  reasoning_tokens?: number
}

/** 对话消息（前端本地维护，含流式状态） */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  /** 秒级时间戳；新推的消息由前端用 Date.now()/1000 填，历史回显用后端值 */
  created_at?: number
  streaming?: boolean
  /** 推理模型的思维链，流式累积；只用于展示，不参与正文 */
  reasoning?: string
  toolCalls?: ToolCallRecord[]
  pendingConfirm?: PendingConfirm
  error?: string
  /** 本条回复的 token 用量（后端在流末尾回传）。model 用于按真实模型计价 */
  usage?: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    reasoning_tokens?: number
    model?: string
  }
}

export interface ToolCallRecord {
  id: string
  name: string
  arguments: Record<string, unknown>
  result?: Record<string, unknown>
}

export interface PendingConfirm {
  id: string
  name: string
  arguments: Record<string, unknown>
  message: string
}

/** 平台元数据（用于界面展示） */
export interface PlatformMeta {
  id: Platform
  name: string
  icon: string
  color: string
  tagline: string
}

// ------------------------------------------------------------------ 模型池

/** 功能槽元信息（后端 FEATURES 的展示层） */
export interface ModelFeature {
  key: string
  name: string
  /** 默认任务档位（模型没配 temperature/max_tokens 时用） */
  task: string
  /** 是否禁止备选链（embedding：换模型 = 旧向量全废，不能靠降级换） */
  no_fallback: boolean
}

export interface PoolMeta {
  features: ModelFeature[]
  protocols: string[]
  capabilities: string[]
}

/** 服务商（api_key 已脱敏） */
export interface ModelProviderRow {
  id: string
  key: string
  name: string
  kind: 'openai' | 'cloudflare'
  base_url: string
  api_key: string
  has_key: boolean
  enabled: boolean
  note: string
  daily_calls_limit: number | null
  daily_token_limit: number | null
  monthly_token_limit: number | null
  rpm_limit: number | null
  quota_reset_tz: 'cst' | 'utc'
  /** 这家服务商默认算什么账（模型自己没标 billing 时的兜底） */
  billing_default?: 'free' | 'paid'
  updated_at: number
}

/** 模型条目 */
export interface ModelCatalogEntry {
  id: string
  provider_key: string
  model_id: string
  label: string
  capabilities: string[]
  protocol: string
  context_window: number | null
  max_tokens: number | null
  temperature: number | null
  supports_tools: boolean
  supports_stream_usage: boolean
  dimensions: number | null
  billing: 'free' | 'paid'
  price_in: number | null
  price_out: number | null
  quota_scope: 'model' | 'provider'
  daily_calls_limit: number | null
  daily_token_limit: number | null
  monthly_token_limit: number | null
  rpm_limit: number | null
  enabled: boolean
  last_error: string
  /** 熔断剩余秒数，>0 表示现在正被跳过 */
  disabled_for: number
  sort: number
  note: string
  usage: Record<string, number>
  /** 我是否在这个服务商上填了自己的 Key */
  using_my_key?: boolean
}

/** 功能槽绑定（含后端解析出的「实际会用谁」） */
export interface ModelBinding {
  feature: string
  name: string
  task: string
  no_fallback: boolean
  primary: string
  fallbacks: string[]
  resolved: string
  resolved_label: string
  primary_label: string
  updated_at: number
}

/** 远端模型清单同步出来的候选 */
export interface SyncCandidate {
  model_id: string
  label: string
  capabilities: string[]
  protocol: string
  /** 池里是否已存在 */
  exists: boolean
}

/** 额度看板一行 */
export interface QuotaRow {
  model: string
  label: string
  provider: string
  billing: string
  /** 'system' = 烧平台 Key；否则是某个用户的 id（烧他自己的 Key） */
  cred_owner: string
  used: Record<string, number>
  limits: Record<string, number>
  ratio: number
}

export interface MyProviderKey {
  provider_key: string
  provider_name: string
  using_my_key: boolean
  api_key: string
  enabled: boolean
  verified_at: number | null
}

/** 流里的额度预警事件（后端 model_pool.take_warnings 产出） */
export interface QuotaWarning {
  type: 'quota_warning'
  level: 'warn' | 'exhausted'
  feature: string
  model: string
  label: string
  ratio: number
  used: Record<string, number> | null
  limits: Record<string, number> | null
  message: string
}

/** 流里的「已切换模型」事件（开流即失败时切下一个候选） */
export interface ModelSwitched {
  type: 'model_switched'
  from: string
  to: string
  /** 人类可读的一句说明，直接展示 */
  message: string
}
