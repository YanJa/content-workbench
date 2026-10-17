/**
 * 发布队列 Store。
 *
 * 分页说明：`items` 只存**当前页**；筛选（status）在后端做，前端不再本地过滤，
 * 否则每页条数对不上。`total` 为当前筛选条件下的总条数（用于分页控件）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import * as queueApi from '@/api/queue'
import type { QueueItem, QueueStats, QueueStatus } from '@/types'

export const useQueueStore = defineStore('queue', () => {
  const items = ref<QueueItem[]>([])
  const stats = ref<QueueStats>({ total: 0, by_status: {}, by_platform: {} })
  const loading = ref(false)
  // 状态多选筛选（空数组 = 全部）；与关键词搜索是 AND 关系
  const filterStatuses = ref<QueueStatus[]>([])
  // 模糊搜索关键词（标题或正文，后端匹配）
  const keyword = ref('')
  // 分页状态
  const page = ref(1)
  const pageSize = ref(20)
  const total = ref(0)

  const pendingCount = computed(() => stats.value.by_status?.ready ?? 0)

  /** 是否还有加工中的行（有则队列页启动定向轮询）。 */
  const hasProcessing = computed(() => items.value.some((i) => i.processing))

  async function refresh() {
    loading.value = true
    try {
      const [res, st] = await Promise.all([
        queueApi.listQueue({
          status: filterStatuses.value.length ? filterStatuses.value.join(',') : 'all',
          q: keyword.value.trim(),
          page: page.value,
          pageSize: pageSize.value,
        }),
        queueApi.queueStats(),
      ])
      items.value = res.items
      total.value = res.total
      stats.value = st
    } finally {
      loading.value = false
    }
  }

  /** 切换状态多选筛选：重置到第 1 页并重新请求（筛选在后端，与搜索 AND）。 */
  async function setStatuses(statuses: QueueStatus[]) {
    filterStatuses.value = statuses
    page.value = 1
    await refresh()
  }

  /** 设置搜索关键词：重置到第 1 页并重新请求（搜索在后端）。 */
  async function setKeyword(k: string) {
    keyword.value = k
    page.value = 1
    await refresh()
  }

  /** 切换页码。 */
  async function setPage(p: number) {
    page.value = p
    await refresh()
  }

  /** 切换每页条数：重置到第 1 页。 */
  async function setPageSize(s: number) {
    pageSize.value = s
    page.value = 1
    await refresh()
  }

  /**
   * 定向刷新：只重拉当前页并**就地更新**处理中标记，不重载统计、不整表替换。
   * 用于加工中任务的轮询——比全量 refresh 轻得多。
   */
  async function refreshProcessingRows() {
    try {
      const res = await queueApi.listQueue({
        status: filterStatuses.value.length ? filterStatuses.value.join(',') : 'all',
        page: page.value,
        pageSize: pageSize.value,
      })
      const map = new Map(res.items.map((it) => [it.id, it]))
      for (const local of items.value) {
        const fresh = map.get(local.id)
        if (fresh && fresh.processing !== local.processing) {
          local.processing = fresh.processing
        }
      }
    } catch {
      // 轮询失败静默，下一轮再试
    }
  }

  async function setStatus(id: string, status: QueueStatus) {
    await queueApi.updateQueueStatus(id, status)
    await refresh()
  }

  async function setChecklist(id: string, checklist: Record<string, boolean>) {
    await queueApi.updateChecklist(id, checklist)
    await refresh()
  }

  async function remove(id: string) {
    await queueApi.deleteQueueItem(id)
    await refresh()
  }

  async function exportMarkdown(): Promise<string> {
    return queueApi.exportQueue()
  }

  return {
    items,
    stats,
    loading,
    filterStatuses,
    keyword,
    page,
    pageSize,
    total,
    pendingCount,
    hasProcessing,
    refresh,
    setStatuses,
    setKeyword,
    setPage,
    setPageSize,
    refreshProcessingRows,
    setStatus,
    setChecklist,
    remove,
    exportMarkdown,
  }
})
