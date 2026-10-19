/**
 * 认证 API：登录 / 注册 / 当前用户。
 *
 * 复用 http 实例（自动带 Authorization 头）；登录与注册请求本身不需要
 * 令牌，拦截器发现本地没有 token 时自然跳过。
 */
import { http } from './http'

/**
 * 角色。**叫 Role 而不是复用聊天消息那个 role** —— types/index.ts 里
 * 早有一个 `role: 'user' | 'assistant' | 'tool' | 'system'`（对话消息角色），
 * 语义完全不同，混在一起迟早出错。
 *
 * 由系统预置的管理员账号持有 admin；注册进来的一律 member。
 */
export type Role = 'admin' | 'member'

/** 配额周期。按**北京时间**切分，不是 UTC —— 否则早上 8 点前「今日用量」对不上。 */
export type QuotaPeriod = 'day' | 'month'

/** 一条生效的配额配置。limit 为 null 表示「不限」（缺失 / 0 / 负数都归一成它）。 */
export interface QuotaConf {
  period: QuotaPeriod
  limit: number | null
}

/** 某个功能在本周期的用量快照（来自 /auth/me 的 usage） */
export interface QuotaUsage extends QuotaConf {
  label: string
  used: number
  unlimited: boolean
  /** 剩余次数；不限时为 null */
  left: number | null
  /** 下次重置的时间戳（秒） */
  reset_at: number
}

export interface AuthUser {
  id: string
  username: string
  created_at: number
  /**
   * 昵称（展示用）。未设置时为 null —— 顶栏 / 个人中心一律
   * `nickname || username` 兜底。**不唯一**，数据隔离靠 id。
   */
  nickname: string | null
  /**
   * 头像签名直链（后端裁剪成的 256×256 JPEG，HMAC 隔离）。null = 未设置，
   * 顶栏 / 个人中心渲染「用户名首字母」圆徽。
   */
  avatar_url: string | null
  /**
   * 邮箱（联系方式）。不用于登录、不唯一索引。null = 未设置；
   * 后端保存时查重（被别的账号占了会 409）。
   */
  email: string | null
  /** 角色：决定侧栏菜单显隐与管理端点能否调用 */
  role: Role
  /** 停用后所有 Bearer 请求当场 401，不等令牌过期 */
  is_active: boolean
  /**
   * 生效的权限点 key 列表（如 'video.render'）。**按钮显隐/置灰的唯一依据**。
   *
   * 这是后端每次请求现算的结果，不进 token：管理员刚改完，下一个 /me 就是新值。
   */
  permissions: string[]
  /** 生效的配额配置：{ feature: { period, limit } } */
  quotas: Record<string, QuotaConf>
  /** 各功能本周期用量：{ feature: QuotaUsage } */
  usage: Record<string, QuotaUsage>
  /** 注册时用的邀请码。空 = 首用户 / 代建 / 预置 admin */
  invite_code: string | null
}

/** 首用户注册时的存量认领摘要（后端 `_claim_legacy_data` 返回） */
export interface ClaimedSummary {
  /** 被改归到新用户的行数（contents/queue_items/conversations 等 7 张表合计） */
  rows: number
  /** 被重写为签名直链的旧 URL 条数 */
  urls: number
  /** 音色目录是否已搬到 owners/<uid>/ 下 */
  voice_dir_moved: boolean
}

export interface TokenOut {
  token: string
  user: AuthUser
  /** 仅首用户注册时非空 */
  claimed?: ClaimedSummary | null
}

export async function login(username: string, password: string): Promise<TokenOut> {
  const { data } = await http.post<TokenOut>('/auth/login', { username, password })
  return data
}

export async function register(
  username: string,
  password: string,
  inviteCode = '',
): Promise<TokenOut> {
  const { data } = await http.post<TokenOut>('/auth/register', {
    username,
    password,
    invite_code: inviteCode,
  })
  return data
}

export async function getMe(): Promise<AuthUser> {
  const { data } = await http.get<AuthUser>('/auth/me')
  return data
}

/** 个人中心保存资料。传了哪个字段就改哪个；空串 = 清空（回落用户名）。 */
export async function updateProfile(patch: {
  nickname?: string
  email?: string
}): Promise<AuthUser> {
  const { data } = await http.put<AuthUser>('/auth/profile', patch)
  return data
}

/**
 * 上传/更换头像。后端会居中裁剪成 256×256 JPEG（透明贴白底），
 * 原格式原尺寸不落地。限制：png/jpg/webp、≤2MB。
 */
export async function uploadAvatar(file: File): Promise<AuthUser> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post<AuthUser>('/auth/avatar', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  })
  return data
}

/** 删除头像，回到「用户名首字母」圆徽。 */
export async function deleteAvatar(): Promise<AuthUser> {
  const { data } = await http.delete<AuthUser>('/auth/avatar')
  return data
}
