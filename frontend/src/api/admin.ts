/**
 * 管理面 API：用户管理 + 全站聚合 + 权限/配额。
 *
 * 全部端点都要求 admin 角色，非管理员会得到后端 403（中文 detail 由
 * http 拦截器统一抛出）。前端的菜单/路由守卫只是提前挡一道，真正的
 * 校验在后端 —— 藏菜单不是权限控制。
 */
import { http } from './http'
import type { QuotaConf, QuotaPeriod, QuotaUsage, Role } from './auth'

export interface AdminUser {
  id: string
  username: string
  created_at: number
  role: Role
  is_active: boolean
  last_login_at: number | null
  /** 该用户名下的内容数：一眼看出谁是空账号 */
  contents: number
  /** 生效的权限点（细粒度权限上线后新增） */
  permissions: string[]
  quotas: Record<string, QuotaConf>
  usage: Record<string, QuotaUsage>
  /** 注册时用的邀请码（渠道统计）；空 = 首用户/代建/预置 admin */
  invite_code: string | null
  /** 来源邀请码的渠道备注（如「小红书投放」）；空 = 无来源/码已删 */
  invite_note: string | null
}

export interface AdminStatsUser {
  id: string
  username: string
  role: Role
  is_active: boolean
  contents: number
  llm_calls: number
  total_tokens: number
  cost: number
}

export interface AdminStats {
  users: { total: number; active: number; admins: number }
  contents: number
  queue_items: number
  render_jobs: number
  llm: { calls: number; total_tokens: number; cost: number }
  by_user: AdminStatsUser[]
}

export interface CreateUserPayload {
  username: string
  password: string
  role?: Role
  /** 代建账号的来源邀请码（可选）。填了进渠道统计，不填 = 无来源。 */
  invite_code?: string
}

export interface UpdateUserPayload {
  role?: Role
  is_active?: boolean
}

/** 用户列表（含内容数）。 */
export async function listUsers(): Promise<AdminUser[]> {
  const { data } = await http.get<AdminUser[]>('/admin/users')
  return data
}

/** 管理员代建账号。邀请码可选（填了当来源，不扣次数）。 */
export async function createUser(payload: CreateUserPayload): Promise<AdminUser> {
  const { data } = await http.post<AdminUser>('/admin/users', payload)
  return data
}

/** 改角色 / 停用 / 恢复启用。不能对自己动手，也不能清空最后一个管理员。 */
export async function updateUser(
  id: string,
  payload: UpdateUserPayload,
): Promise<AdminUser> {
  const { data } = await http.patch<AdminUser>(`/admin/users/${id}`, payload)
  return data
}

/** 重置某个普通用户的密码（管理员账号的密码不走此旁路，后端会 400）。 */
export async function resetUserPassword(id: string, password: string): Promise<AdminUser> {
  const { data } = await http.post<AdminUser>(`/admin/users/${id}/reset-password`, {
    password,
  })
  return data
}

/** 删除一个从未产生过任何数据的空账号。有数据/管理员/自己会被后端 400 拒绝。 */
export async function deleteUser(id: string): Promise<void> {
  await http.delete(`/admin/users/${id}`)
}

/** 全站聚合统计：只报数字，不报内容。 */
export async function getAdminStats(): Promise<AdminStats> {
  const { data } = await http.get<AdminStats>('/admin/stats')
  return data
}

// ---------------------------------------------------------------- 权限与配额
/** 权限点目录里的一项 */
export interface PermissionItem {
  group: string
  key: string
  label: string
}

export interface QuotaFeatureItem {
  key: string
  label: string
}

/** 某个角色的模板快照。permissions 为 null = 全部开放且固定（admin 专属）。 */
export interface RoleSnapshot {
  permissions: string[] | null
  locked: boolean
  quotas: Record<string, QuotaConf>
}

export interface PermissionCatalog {
  permissions: PermissionItem[]
  quota_features: QuotaFeatureItem[]
  periods: { key: QuotaPeriod; label: string }[]
  roles: Record<Role, RoleSnapshot>
}

export interface RolePermPayload {
  /** 省略表示不动权限清单（只改配额时可以只传 quotas） */
  permissions?: string[]
  quotas?: Record<string, QuotaConf>
}

export interface UserOverridePayload {
  /** { grant: [...], deny: [...] } —— 整体替换该用户的覆盖块 */
  perm_override?: { grant: string[]; deny: string[] }
  /** 个人配额覆盖：{ feature: { period, limit } }，limit 为 null = 不限 */
  quota_override?: Record<string, QuotaConf>
}

/** 权限点目录 + 各角色当前模板。管理页「权限模板」卡据此渲染勾选矩阵。 */
export async function getPermissionCatalog(): Promise<PermissionCatalog> {
  const { data } = await http.get<PermissionCatalog>('/admin/permissions')
  return data
}

/**
 * 改某个角色的权限清单 / 配额。
 *
 * admin 角色传 permissions 会被后端 400（权限固定全开，防自锁）；
 * admin 的配额可以照改 —— 成本闸门对管理员一视同仁。
 */
export async function putRolePermissions(
  role: Role,
  payload: RolePermPayload,
): Promise<RoleSnapshot> {
  const { data } = await http.put<RoleSnapshot>(`/admin/role-permissions/${role}`, payload)
  return data
}

/** 给单个用户开/关权限点、改配额（角色默认之上的增量覆盖）。 */
export async function putUserOverrides(
  id: string,
  payload: UserOverridePayload,
): Promise<AdminUser> {
  const { data } = await http.put<AdminUser>(`/admin/users/${id}/overrides`, payload)
  return data
}

/** 人工重置某人的用量计数（只清计数，不清配置）。feature 省略 = 全部。 */
export async function resetUserQuota(id: string, feature?: string): Promise<AdminUser> {
  const { data } = await http.post<AdminUser>(`/admin/users/${id}/quota-reset`, {
    feature: feature ?? null,
  })
  return data
}

// ---------------------------------------------------------------- 邀请码管理
export interface InviteCodeItem {
  id: string
  code: string
  note: string
  /** null = 不限次 */
  max_uses: number | null
  used: number
  disabled: boolean
  created_at: number
  /** 用尽（used >= max_uses，max_uses 非空时） */
  exhausted: boolean
}

export interface InviteCodePayload {
  /** 留空 = 后端随机生成 */
  code?: string
  note?: string
  /** 留空 / 0 / 负数 = 不限次 */
  max_uses?: number | null
}

/** 全部邀请码，最新在前。 */
export async function listInviteCodes(): Promise<InviteCodeItem[]> {
  const { data } = await http.get<InviteCodeItem[]>('/admin/invite-codes')
  return data
}

/** 生成一条邀请码（可自定义或随机，可限次或不限）。 */
export async function createInviteCode(payload: InviteCodePayload): Promise<InviteCodeItem> {
  const { data } = await http.post<InviteCodeItem>('/admin/invite-codes', payload)
  return data
}

/** 修改邀请码的渠道备注（note）。只改备注，不碰码本身 / 次数 / 停用态。 */
export async function updateInviteCode(id: string, note: string): Promise<InviteCodeItem> {
  const { data } = await http.patch<InviteCodeItem>(`/admin/invite-codes/${id}`, { note })
  return data
}

/** 停用一条邀请码。停用后立即不能再注册。 */
export async function disableInviteCode(id: string): Promise<InviteCodeItem> {
  const { data } = await http.post<InviteCodeItem>(`/admin/invite-codes/${id}/disable`)
  return data
}

/** 重新启用一条被停用的邀请码。 */
export async function enableInviteCode(id: string): Promise<InviteCodeItem> {
  const { data } = await http.post<InviteCodeItem>(`/admin/invite-codes/${id}/enable`)
  return data
}

/** 删除一条从没被用过的邀请码（用过的码是渠道统计的一部分，不能删）。 */
export async function deleteInviteCode(id: string): Promise<void> {
  await http.delete(`/admin/invite-codes/${id}`)
}
