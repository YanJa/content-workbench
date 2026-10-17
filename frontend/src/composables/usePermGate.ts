/**
 * 按钮级权限 + 配额的统一判定。
 *
 * 为什么值得单独抽一个文件：这五个判断（有没有权限、配额用没用完、为什么
 * 不能点、用完怎么提示）在对话/视频/排版/生图/队列五个页面都要用一遍，
 * 各写一遍必然写歪 —— 尤其是提示文案，五处不一致用户会以为系统在抽风。
 *
 * 三条边界，用之前必须知道：
 *
 * 1. **这只是 UX，不是权限控制。** 后端每个端点都有自己的 `require_use`
 *    依赖，前端把按钮藏了/灰了，照样挡不住有人拿 curl 直接打。这里的存在
 *    意义是「少弹一个 403、少一个点了没反应的按钮」。
 * 2. **admin 永远放行。** `auth.can()` 对 admin 直接返回 true，与后端口径
 *    一致（管理员权限固定全开，防自锁）。但配额对 admin 也生效 —— 那是
 *    成本闸门不是权限，后端 `require_use` 里 admin 照样扣。
 * 3. **配额快照有至多 60 秒延迟。** 静默刷新 + 403/429 时立即刷新兜着，
 *    所以这里显示的「还剩几次」可能比服务端慢一分钟。真正的拦截在后端，
 *    慢一分钟只影响提示，不影响正确性。
 */
import { computed } from 'vue'

import { useAuthStore } from '@/stores/auth'

export interface PermGateOptions {
  /** 需要的权限点，如 'video.render' */
  perm?: string
  /** 要扣减的配额功能，如 'video' */
  quota?: string
}

export function usePermGate(options: PermGateOptions = {}) {
  const auth = useAuthStore()

  /** 有没有这个功能的权限 */
  const allowed = computed(() => !options.perm || auth.can(options.perm))

  /** 配额是否已用尽（「不限」永远 false） */
  const exhausted = computed(() => (options.quota ? auth.quotaExhausted(options.quota) : false))

  /** 按钮是否该禁用 */
  const blocked = computed(() => !allowed.value || exhausted.value)

  /** 禁用原因。空字符串表示没被禁用 —— 直接拿去做 tooltip/content */
  const reason = computed<string>(() => {
    if (!allowed.value) return '没有该功能的使用权限，请联系管理员'
    if (exhausted.value) {
      const snap = options.quota ? auth.quotaOf(options.quota) : undefined
      const period = snap?.period === 'month' ? '本月' : '今日'
      return `${period}${snap?.label ?? '该功能'}次数已用完（${snap?.used ?? 0}/${snap?.limit ?? 0}）`
    }
    return ''
  })

  /** 剩余次数；null = 不限。给按钮文案用（如「生成视频（剩 2）」） */
  const left = computed<number | null>(() =>
    options.quota ? auth.quotaLeft(options.quota) : null,
  )

  return { allowed, exhausted, blocked, reason, left }
}
