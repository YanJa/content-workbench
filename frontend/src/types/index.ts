/**
 * 全局类型定义。
 * 与后端 app/schemas/models.py 一一对应，改后端字段时记得同步这里。
 */

export type Platform = 'gzh' | 'xiaohongshu' | 'douyin'

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
    }
  | { type: 'done'; finish_reason: string }
  | { type: 'error'; message: string }

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
  streaming?: boolean
  /** 推理模型的思维链，流式累积；只用于展示，不参与正文 */
  reasoning?: string
  toolCalls?: ToolCallRecord[]
  pendingConfirm?: PendingConfirm
  error?: string
  /** 本条回复的 token 用量（后端在流末尾回传） */
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number; reasoning_tokens?: number }
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
