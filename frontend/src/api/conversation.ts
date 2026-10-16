/** 会话管理接口。 */
import { http } from './http'
import type { Conversation, ConversationDetail } from '@/types'

export async function listConversations(): Promise<Conversation[]> {
  const { data } = await http.get('/chat/conversations')
  return data
}

export async function createConversation(
  title = '新对话',
  scene = 'chat',
): Promise<Conversation> {
  const { data } = await http.post('/chat/conversations', { title, scene })
  return data
}

export async function getConversation(id: string): Promise<ConversationDetail> {
  const { data } = await http.get(`/chat/conversations/${id}`)
  return data
}

export async function renameConversation(
  id: string,
  title: string,
): Promise<Conversation> {
  const { data } = await http.patch(`/chat/conversations/${id}`, { title })
  return data
}

export async function deleteConversation(id: string): Promise<void> {
  await http.delete(`/chat/conversations/${id}`)
}
