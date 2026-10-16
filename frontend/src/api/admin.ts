/**
 * 管理面 API：用户管理 + 全站聚合。
 *
 * 全部端点都要求 admin 角色，非管理员会得到后端 403（中文 detail 由
 * http 拦截器统一抛出）。前端的菜单/路由守卫只是提前挡一道，真正的
 * 校验在后端 —— 藏菜单不是权限控制。
 */
import { http } from './http'
import type { Role } from './auth'

export interface AdminUser {
  id: string
  username: string
  created_at: number
  role: Role
  is_active: boolean
  last_login_at: number | null
  /** 该用户名下的内容数：一眼看出谁是空账号 */
  contents: number
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

/** 管理员代建账号 —— 绕过邀请码（邀请码是给自助注册用的门槛）。 */
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

/** 全站聚合统计：只报数字，不报内容。 */
export async function getAdminStats(): Promise<AdminStats> {
  const { data } = await http.get<AdminStats>('/admin/stats')
  return data
}
