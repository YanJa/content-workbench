/**
 * 对话 Store。
 * 负责：消息列表、SSE 流式接收、工具调用状态、人工确认。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { streamChat } from '@/api/chat'
import * as convApi from '@/api/conversation'
import { confirmTool as confirmToolApi } from '@/api/tools'
import type { ChatMessage, Conversation, PendingConfirm, Platform, ToolCallRecord } from '@/types'

let seq = 0
const uid = () => `m_${Date.now()}_${++seq}`

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([])
  const currentId = ref<string | null>(null)
  const messages = ref<ChatMessage[]>([])
  const streaming = ref(false)
  const platform = ref<Platform | null>(null)
  /**
   * 本轮是否带上知识库素材（默认开）。
   *
   * 关掉的意义：素材是「相关就参考」，偶尔会带偏——比如要 AI 完全自由发挥、
   * 或本轮话题跟库存素材无关时。开关只影响**本轮请求**，不是全局设置：
   * 写稿是逐轮的事，用户要的是「这一句不带素材」。
   */
  const useKnowledge = ref(true)
  /**
   * 本轮是否挂载技能包（默认开）。
   *
   * 关掉的意义：技能会「按它那套做法来」，偶尔不是你要的——比如想让 AI
   * 自由发挥、或某个技能的规矩跟本轮任务冲突。关掉后这一轮既不注入技能
   * 清单、也不提供 activate_skill 工具，等于临时把它们全停用。同样只作用于
   * **本轮请求**。
   */
  const useSkills = ref(true)

  let controller: AbortController | null = null

  async function loadConversations() {
    conversations.value = await convApi.listConversations()
  }

  async function openConversation(id: string) {
    const detail = await convApi.getConversation(id)
    currentId.value = detail.id
    messages.value = detail.messages
      // system 是提示词不是对话内容；tool 行是历史遗留形态（工具痕迹已并入
      // assistant 行的 tool_calls.calls），单独渲染会变成一串没头没尾的 JSON
      .filter((m) => m.role !== 'system' && m.role !== 'tool')
      .map((m) => {
        // 展示型工具痕迹 → 前端 ToolCallRecord[]（流式态与回显态同一结构）
        const calls = (m.tool_calls as { calls?: ToolCallRecord[] } | null)?.calls
        const usage =
          m.prompt_tokens || m.completion_tokens
            ? {
                prompt_tokens: m.prompt_tokens ?? 0,
                completion_tokens: m.completion_tokens ?? 0,
                total_tokens: (m.prompt_tokens ?? 0) + (m.completion_tokens ?? 0),
              }
            : undefined
        return {
          id: m.id,
          role: m.role as ChatMessage['role'],
          content: m.content,
          reasoning: m.reasoning || undefined,
          // 模型调用失败的错误信息：后端随该轮回复落库，历史回显继续显示红条
          error: m.error || undefined,
          toolCalls: calls?.length ? calls : undefined,
          usage,
        }
      })
  }

  function newConversation() {
    currentId.value = null
    messages.value = []
  }

  /**
   * 清空全部状态（登出/切换用户时调用）。
   *
   * 2026-10-19 实测 bug：登出只清了 auth store，本 store 的 messages /
   * currentId / conversations 原样保留 —— 下个用户登录进对话页，看到的是
   * 上家的提问与回答（会话列表却是新的，因为列表按 owner 重新拉过）。
   * 更危险的是残留的 currentId 属于上家，此时发消息会把 conversation_id
   * 指向上家的会话。所以登出必须整体重置，顺带中断进行中的流式请求。
   */
  function reset() {
    controller?.abort()
    controller = null
    streaming.value = false
    conversations.value = []
    currentId.value = null
    messages.value = []
    platform.value = null
  }

  async function removeConversation(id: string) {
    await convApi.deleteConversation(id)
    if (currentId.value === id) newConversation()
    await loadConversations()
  }

  /** 重命名会话。返回服务端最终生效的标题（空白会被回落为「新对话」）。
   *  就地更新本地项，避免整列表重拉导致列表滚动位置跳动。 */
  async function renameConversation(id: string, title: string) {
    const updated = await convApi.renameConversation(id, title)
    const hit = conversations.value.find((c) => c.id === id)
    if (hit) hit.title = updated.title
    return updated.title
  }

  /** 发送消息并接收流式回复 */
  async function send(text: string, scene?: Platform | null) {
    if (!text.trim() || streaming.value) return

    messages.value.push({ id: uid(), role: 'user', content: text })

    const assistant: ChatMessage = {
      id: uid(),
      role: 'assistant',
      content: '',
      streaming: true,
      toolCalls: [],
    }
    messages.value.push(assistant)
    streaming.value = true

    controller = new AbortController()

    await streamChat(
      {
        conversation_id: currentId.value,
        message: text,
        platform: scene ?? platform.value,
        enable_tools: true,
        // 让这条回复的 id 落库时当主键：保存到队列上报的 msg_id 才能与
        // 历史消息 id 对上，队列页「回到对话」才定位得到这条回复
        assistant_msg_id: assistant.id,
        use_knowledge: useKnowledge.value,
        use_skills: useSkills.value,
      },
      {
        onEvent: (evt) => {
          const last = messages.value[messages.value.length - 1]
          if (!last) return

          switch (evt.type) {
            case 'conversation':
              if (!currentId.value) {
                currentId.value = evt.conversation_id
                void loadConversations()
              }
              break

            case 'delta':
              last.content += evt.content
              break

            case 'reasoning':
              // 推理模型的思维链：单独累积，正文区不显示
              last.reasoning = (last.reasoning ?? '') + evt.content
              break

            case 'usage':
              last.usage = evt.usage
              break

            case 'tool_call':
              last.toolCalls?.push({
                id: evt.id,
                name: evt.name,
                arguments: evt.arguments,
              })
              break

            case 'tool_result': {
              const rec = last.toolCalls?.find((t) => t.id === evt.id)
              if (rec) rec.result = evt.result
              break
            }

            case 'tool_confirm':
              last.pendingConfirm = {
                id: evt.id,
                name: evt.name,
                arguments: evt.arguments,
                message: evt.message,
              } satisfies PendingConfirm
              break

            case 'error':
              last.error = evt.message
              break

            case 'done':
              last.streaming = false
              break
          }
        },
        onError: (err) => {
          const last = messages.value[messages.value.length - 1]
          if (last) {
            last.error = err.message
            last.streaming = false
          }
        },
        onClose: () => {
          streaming.value = false
          const last = messages.value[messages.value.length - 1]
          if (last) last.streaming = false
        },
      },
      controller.signal,
    )
  }

  /** 用户确认危险工具后执行 */
  async function confirm(convId: string, pending: PendingConfirm, msgIndex: number) {
    await confirmToolApi({
      conversation_id: convId,
      tool_call_id: pending.id,
      tool_name: pending.name,
      arguments: pending.arguments,
    })
    const msg = messages.value[msgIndex]
    if (msg) {
      msg.pendingConfirm = undefined
      msg.toolCalls?.push({
        id: pending.id,
        name: pending.name,
        arguments: pending.arguments,
        result: { status: 'executed' },
      })
    }
  }

  function reject(msgIndex: number) {
    const msg = messages.value[msgIndex]
    if (msg) {
      msg.pendingConfirm = undefined
      msg.content += '\n\n（已取消该操作）'
    }
  }

  function stop() {
    controller?.abort()
    streaming.value = false
  }

  return {
    conversations,
    currentId,
    messages,
    streaming,
    platform,
    useKnowledge,
    useSkills,
    loadConversations,
    openConversation,
    newConversation,
    reset,
    removeConversation,
    renameConversation,
    send,
    confirm,
    reject,
    stop,
  }
})
