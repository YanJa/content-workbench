/** 应用设置接口：公众号账号管理。 */
import { http } from './http'
import type { WeChatAccount } from '@/types'

export interface WeChatAccountPayload {
  name: string
  appid: string
  secret: string
  homepage?: string
  /** 对外号名（替换 {{号名}}） */
  gzh_name?: string
  /** 关注卡副标（替换 {{slogan}}） */
  slogan?: string
  is_default?: boolean
}

/** 公众号列表（默认号排最前）。 */
export async function listWeChatAccounts(): Promise<WeChatAccount[]> {
  const { data } = await http.get('/settings/wechat')
  return data
}

export async function createWeChatAccount(
  payload: WeChatAccountPayload,
): Promise<WeChatAccount> {
  const { data } = await http.post('/settings/wechat', payload)
  return data
}

/** 更新；secret 留空 = 不改。 */
export async function updateWeChatAccount(
  id: string,
  patch: Partial<WeChatAccountPayload>,
): Promise<WeChatAccount> {
  const { data } = await http.put(`/settings/wechat/${id}`, patch)
  return data
}

export async function deleteWeChatAccount(id: string): Promise<void> {
  await http.delete(`/settings/wechat/${id}`)
}

/** 测试连通性（拉 access_token 验证凭证 + IP 白名单）。 */
export async function checkWeChatAccount(
  id: string,
): Promise<{ ok: boolean; error?: string; errcode?: number }> {
  const { data } = await http.post(`/settings/wechat/${id}/check`)
  return data
}

/** 设为默认推送号。 */
export async function setDefaultWeChatAccount(id: string): Promise<WeChatAccount> {
  const { data } = await http.post(`/settings/wechat/${id}/default`)
  return data
}

/** 上传公众号二维码（文末关注卡「扫码 / 长按关注」用）。账号需已存在。 */
export async function uploadWeChatQrcode(id: string, file: File): Promise<WeChatAccount> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await http.post(`/settings/wechat/${id}/qrcode`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  })
  return data
}

/** 作者信息（排版时用来替换正文里的 {{作者名}} / {{一句话简介}}）。 */
export interface AuthorInfo {
  author_name: string
  author_intro: string
}

export async function getAuthorInfo(): Promise<AuthorInfo> {
  const { data } = await http.get('/settings/author')
  return data
}

export async function updateAuthorInfo(patch: AuthorInfo): Promise<{ ok: boolean }> {
  const { data } = await http.put('/settings/author', patch)
  return data
}

/** 对话供应商：deepseek（默认，全功能）/ cloudflare（Workers AI）。 */
export interface ChatProviderInfo {
  provider: 'deepseek' | 'cloudflare'
  model: string
  cloudflare_ready: boolean
}

export async function getChatProvider(): Promise<ChatProviderInfo> {
  const { data } = await http.get('/settings/chat-provider')
  return data
}

export async function updateChatProvider(
  patch: { provider: 'deepseek' | 'cloudflare'; model?: string },
): Promise<{ ok: boolean; provider: string; model: string }> {
  const { data } = await http.put('/settings/chat-provider', patch)
  return data
}

/** Cloudflare 账号下可用于对话的模型清单（[{name, label}]）。 */
export async function getCloudflareModels(): Promise<{ name: string; label: string }[]> {
  const { data } = await http.get('/settings/cloudflare-models')
  return data.models ?? []
}
