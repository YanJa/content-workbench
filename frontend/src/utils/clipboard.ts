/**
 * 剪贴板写入。
 *
 * 为什么单独一个文件：知乎/公众号这类富文本编辑器是「粘贴什么就渲染什么」，
 * 只给纯文本用户就得自己重排一遍标题和列表。所以复制要能同时写
 * `text/html`（给编辑器）和 `text/plain`（给记事本、微信聊天框）。
 *
 * ClipboardItem 不是所有浏览器都支持（旧 Firefox / 非安全上下文），
 * 所以这里明确区分三种结果，让调用方能说清楚「复制成了哪种」，
 * 而不是一律弹「已复制」骗人。
 */
export type CopyResult = 'rich' | 'plain' | 'fail'

/** 同时写富文本与纯文本。不支持富文本时退化为纯文本。 */
export async function copyHtml(html: string, plain: string): Promise<CopyResult> {
  const text = plain || html
  try {
    if (
      typeof ClipboardItem !== 'undefined' &&
      navigator.clipboard &&
      typeof (navigator.clipboard as Clipboard & { write?: unknown }).write === 'function'
    ) {
      const item = new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain' }),
      })
      await navigator.clipboard.write([item])
      return 'rich'
    }
  } catch {
    // 落到下面走纯文本
  }
  try {
    await navigator.clipboard.writeText(text)
    return 'plain'
  } catch {
    return 'fail'
  }
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
