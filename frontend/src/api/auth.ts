/**
 * 认证 API：登录 / 注册 / 当前用户。
 *
 * 复用 http 实例（自动带 Authorization 头）；登录与注册请求本身不需要
 * 令牌，拦截器发现本地没有 token 时自然跳过。
 */
import { http } from './http'

export interface AuthUser {
  id: string
  username: string
  created_at: number
}

export interface TokenOut {
  token: string
  user: AuthUser
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
