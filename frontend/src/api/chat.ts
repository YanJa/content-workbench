/**
 * SSE 流式对话客户端。
 *
 * 为什么不用 EventSource：
 *   EventSource 只支持 GET，无法提交 JSON body。
 *   所以用 fetch + ReadableStream 手动解析 SSE 协议。
 *
 * 用法：
 *   const ac = new AbortController()
 *   await streamChat(req, (evt) => { ... }, ac.signal)
 */
import { getApiBase } from './config'
import { getToken } from './token'
import type { ChatRequest, SSEEvent } from '@/types'

export interface StreamHandlers {
  onEvent: (evt: SSEEvent) => void
  onError?: (err: Error) => void
  onClose?: () => void
}

export async function streamChat(
  req: ChatRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  try {
    // 每次发送时取当前地址，用户在设置页改完立即生效
    const token = getToken()
    const resp = await fetch(`${getApiBase()}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // SSE 走原生 fetch（不经 axios 拦截器），令牌要自己带
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(req),
      signal,
    })

    if (!resp.ok) {
      if (resp.status === 401) {
        throw new Error('登录已过期，请刷新页面重新登录')
      }
      throw new Error(`HTTP ${resp.status}: ${await resp.text().catch(() => '')}`)
    }
    if (!resp.body) throw new Error('响应无数据流')

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    // 逐块读取，按 SSE 的 \n\n 分隔事件
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      const parts = buffer.split('\n\n')
      // 最后一段可能不完整，留到下一轮
      buffer = parts.pop() ?? ''

      for (const part of parts) {
        const line = part.trim()
        if (!line.startsWith('data:')) continue
        const payload = line.slice(5).trim()
        if (!payload) continue
        try {
          handlers.onEvent(JSON.parse(payload) as SSEEvent)
        } catch (e) {
          console.warn('[SSE] 解析失败', payload, e)
        }
      }
    }
  } catch (err) {
    // 用户主动中断不算错误
    if (err instanceof DOMException && err.name === 'AbortError') {
      handlers.onClose?.()
      return
    }
    handlers.onError?.(err instanceof Error ? err : new Error(String(err)))
  } finally {
    handlers.onClose?.()
  }
}
