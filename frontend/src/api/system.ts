import { http } from './http'

/** 运行时信息（版本 / 运行时长 / 数据库大小） */
export interface RuntimeInfo {
  version: string
  env: string
  started_at: number
  uptime_secs: number
  uptime: string
  db_size_bytes: number | null
  pid: number
}

/** 外部通道状态灯：ok=凭证就位；detail 是一句话说明 */
export interface ChannelStatus {
  ok: boolean
  detail: string
  used?: number
  quota?: number
}

/** 模型池里一个功能槽的绑定与额度水位 */
export interface PoolBinding {
  feature: string
  label: string
  primary: string | null
  fallbacks?: number
  quota?: { metric: string; used: number; limit: number; ratio: number } | null
}

export interface PoolAlert {
  level: 'warning' | 'danger'
  model: string
  ratio: number
  message: string
}

/** 最近异常（进程内环形缓冲，重启即清空） */
export interface RecentError {
  ts: number
  source: string
  message: string
  detail: string
}

export interface HealthDetail {
  runtime: RuntimeInfo
  channels: { wechat: ChannelStatus; zhihu: ChannelStatus; cloudflare: ChannelStatus }
  pool: { providers: { key: string; name: string; kind: string }[]; model_count: number; bindings: PoolBinding[]; alerts: PoolAlert[] }
  recent_errors: RecentError[]
}

/**
 * 服务健康详细数据。只读、不拨号：外部通道只查凭证就位与本地计数，
 * 真实连通性测试在各卡片的「检测」按钮上。
 */
export async function getHealthDetail(): Promise<HealthDetail> {
  const { data } = await http.get('/system/health-detail')
  return data
}
