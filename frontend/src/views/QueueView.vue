<script setup lang="ts">
/** 发布队列页：成品管理 + 发布前检查清单。 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NCard,
  NDataTable,
  NInput,
  NModal,
  NCheckbox,
  NSelect,
  NPagination,
  NSpace,
  NTag,
  NTooltip,
  NAlert,
  NEmpty,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'

import { useQueueStore } from '@/stores/queue'
import { confirmTool } from '@/api/tools'
import { cancelProcessing } from '@/api/layout'
import { getContent, saveToQueue } from '@/api/queue'
import type { QueueItem, QueueStatus, ContentItem } from '@/types'
import LayoutPanel from '@/components/LayoutPanel.vue'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'

const queue = useQueueStore()
const message = useMessage()
const dialog = useDialog()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/**
 * 推送草稿的门控：权限 wechat.push + 配额 wechat_push。
 *
 * 与 LayoutPanel 里那个是同一个功能的两处入口（队列页 / 加工面板），
 * 两边都接上门控，否则用户在队列页看到的是能点的按钮、在加工面板看到
 * 的是灰的，会以为页面坏了。
 */
const pushGate = usePermGate({ perm: 'wechat.push', quota: 'wechat_push' })

/** 从对话页跳来时带的 content_id，用于定位并高亮对应行 */
const highlightId = ref('')

/**
 * 「← 返回对话」闭环条：从对话页跳来（query 带 from_conversation_id），
 * 或被高亮的那行本身有来源会话时，给出回到会话的入口。
 */
const returnConvId = ref('')
const returnMsgId = ref('')

const checkVisible = ref(false)
const checkItem = ref<QueueItem | null>(null)
const checks = ref<Record<string, boolean>>({})
/** 已发布 = 任务完成，发布检查转为只读（只能看、不能改勾选/保存） */
const checkReadonly = computed(() => checkItem.value?.status === 'published')
const pushing = ref(false)

// 加工排版面板
const layoutShow = ref(false)
const layoutContent = ref<ContentItem | null>(null)
/** 已发布任务的排版面板为只读「排版详情」（由队列状态驱动，见 LayoutPanel 的 readonly） */
const layoutReadonly = ref(false)

const statusMeta: Record<QueueStatus, { label: string; type: 'default' | 'warning' | 'success' | 'info' }> = {
  draft: { label: '待加工', type: 'default' },
  ready: { label: '待发布', type: 'warning' },
  published: { label: '已发布', type: 'success' },
  archived: { label: '已归档', type: 'info' },
}

const platformName: Record<string, string> = {
  gzh: '📰 公众号',
  xiaohongshu: '📕 小红书',
  douyin: '🎵 抖音',
}

/** 加工中标记 → 展示文案（'' 表示空闲） */
const PROCESSING_LABEL: Record<string, string> = {
  layout: '排版中',
  image: '生图中',
}

/** 各平台的发布路径与检查项（与后端 buildPublishChecklist 对应） */
const CHECKLISTS: Record<string, { channel: string; path: string; items: { key: string; text: string }[] }> = {
  gzh: {
    channel: '公众号草稿箱',
    path: '生成排版成品 → 推送到草稿箱 → 到公众号后台核对 → 手动群发',
    items: [
      { key: 'title', text: '标题已定，前 12 字有钩子' },
      { key: 'cover', text: '封面图已准备（2.35:1 与 1:1 两版）' },
      { key: 'paste', text: '排版成品已推送草稿箱或复制到后台' },
      { key: 'preview', text: '已用手机预览确认排版（重点看图片与卡片）' },
      { key: 'summary', text: '摘要已填（120 字内）' },
      { key: 'original', text: '原创声明 / 转载白名单已处理' },
    ],
  },
  xiaohongshu: {
    channel: '小红书 App',
    path: '复制文案 → 导出配图 → 到小红书 App 发布 → 回来标记已发',
    items: [
      { key: 'cover', text: '封面图已做（3:4，首图带大字标题）' },
      { key: 'title', text: '标题 20 字内且含 emoji 或「｜」' },
      { key: 'imgs', text: '配图 3-9 张已导出' },
      { key: 'tags', text: '话题标签已核对（含 3 个长尾词）' },
      { key: 'loc', text: '需要的话已挂位置 / 是否含商业报备' },
    ],
  },
  douyin: {
    channel: '抖音 App',
    path: '复制口播稿 → 导出成片 → 到抖音发布 → 回来标记已发',
    items: [
      { key: 'video', text: '成片已导出（9:16，时长符合预期）' },
      { key: 'cover', text: '视频封面帧已选定' },
      { key: 'caption', text: '发布文案 55 字内，含互动引导' },
      { key: 'tags', text: '话题含 1 个当期热点词' },
      { key: 'music', text: '背景音乐版权已确认（用平台曲库）' },
    ],
  },
}

const columns = computed<DataTableColumns<QueueItem>>(() => [
  {
    title: '标题',
    key: 'title',
    ellipsis: { tooltip: true },
  },
  {
    title: '平台',
    key: 'platform',
    width: 110,
    render: (row) => platformName[row.platform] ?? row.platform,
  },
  {
    title: '状态',
    key: 'status',
    width: 130,
    render: (row) => {
      // 加工中（排版/生图）优先展示，用加载态标签提示「正在干活，别重复点」
      if (row.processing) {
        return h(
          NTag,
          { size: 'small', type: 'info', bordered: false, loading: true },
          { default: () => PROCESSING_LABEL[row.processing!] ?? '加工中' },
        )
      }
      const m = statusMeta[row.status] ?? { label: row.status, type: 'default' as const }
      return h(NTag, { size: 'small', type: m.type, bordered: false }, { default: () => m.label })
    },
  },
  {
    title: '推送',
    key: 'wechat_media_id',
    width: 100,
    render: (row) => {
      if (!row.wechat_media_id) {
        return h('span', { style: 'color:var(--app-text-3);font-size:12px' }, '—')
      }
      // 已推送：绿色标签常驻显示，tooltip 说明下一步去哪（微信后台预览/群发）
      return h(
        NTooltip,
        { trigger: 'hover' },
        {
          trigger: () =>
            h(
              NTag,
              { size: 'small', type: 'success', bordered: false },
              { default: () => '已推送' },
            ),
          default: () => '已推送到公众号草稿箱，可去微信后台预览或群发',
        },
      )
    },
  },
  {
    title: '创建时间',
    key: 'created_at',
    width: 140,
    render: (row) => new Date(row.created_at * 1000).toLocaleDateString('zh-CN'),
  },
  {
    title: '操作',
    key: 'actions',
    width: 320,
    render: (row) => {
      // 加工中（排版/生图）禁用所有会改状态的操作，避免重复触发/冲突
      const busy = !!row.processing
      const isGzh = row.platform === 'gzh'
      const items = [
        // 加工排版 / 查看排版：加工中时禁用，tooltip 显示当前加工阶段
        actionBtn(
          'edit',
          busy
            ? `加工中：${PROCESSING_LABEL[row.processing!] ?? '排版中'}`
            : row.status === 'published'
              ? '查看排版'
              : '加工排版',
          () => openLayout(row),
          { type: row.status === 'published' ? 'default' : 'primary', disabled: busy },
        ),
        // 发布检查
        actionBtn('search', '发布检查', () => openCheck(row), { disabled: busy }),
        // 推微信草稿箱（公众号）/ 复制文案（其它平台）：额度耗尽时禁用并说明
        row.wechat_media_id
          ? null
          : isGzh && pushGate.blocked.value
            ? actionBtn('send', '无法推送：今日公众号推送额度已用完', () => onPushDraft(row), {
                type: 'primary',
                disabled: true,
              })
            : actionBtn(
                isGzh ? 'send' : 'copy',
                isGzh
                  ? `推微信草稿箱${pushGate.left.value === null ? '' : `（剩 ${pushGate.left.value}）`}`
                  : '复制文案',
                () => onPushDraft(row),
                {
                  type: isGzh ? 'primary' : 'default',
                  disabled: busy || (isGzh && pushGate.blocked.value),
                },
              ),
        // 标记已发：公众号必须先推到微信草稿箱（wechat_media_id 非空）才能标记，
        // 否则推完草稿箱前就标记完成，等于绕过合规发布流程
        row.status !== 'published'
          ? actionBtn(
              'check-circle',
              isGzh && !row.wechat_media_id
                ? '请先推送到微信草稿箱，再标记完成'
                : '标记为已发布',
              () => markPublished(row),
              { disabled: busy || (isGzh && !row.wechat_media_id) },
            )
          : null,
        // 删除
        actionBtn('trash', '删除该内容', () => onDelete(row), { type: 'error', disabled: busy }),
        // 来源是某条对话 → 跳回会话并定位到那条 AI 回复（非会话来源没有此字段）
        row.conversation_id
          ? actionBtn('arrow-left', '回到来源对话', () => goChat(row), { disabled: busy })
          : null,
        // 加工中卡住时的唯一出口：解除 processing 标记，恢复其它按钮可点
        busy
          ? actionBtn('close', '取消加工（解除卡死状态）', () => onCancelProcessing(row), {
              type: 'warning',
            })
          : null,
      ].filter(Boolean) as VNode[]
      return h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, items)
    },
  },
])

import { h, type VNode } from 'vue'
import { actionBtn } from '@/utils/actionBtn'
import IconTextBtn from '@/components/IconTextBtn.vue'

/**
 * 「回到对话」：跳回来源会话并定位到那条 AI 回复。
 * 带上 content_id，会话页的「返回发布队列」据此回跳并再次高亮该行 —— 双向闭环。
 */
function goChat(row: QueueItem) {
  router.push({
    name: 'chat',
    query: {
      conversation_id: row.conversation_id!,
      msg_id: row.msg_id || undefined,
      content_id: row.content_id,
    },
  })
}

/** 关闭「返回对话」提示条（不影响行高亮） */
function dismissReturnBar() {
  returnConvId.value = ''
  returnMsgId.value = ''
}

/** 提示条上的「返回对话」：用提示条自己记下的来源会话/消息 */
function goChatFromBar() {
  router.push({
    name: 'chat',
    query: {
      conversation_id: returnConvId.value,
      msg_id: returnMsgId.value || undefined,
      content_id: highlightId.value || undefined,
    },
  })
}

/** 打开加工排版面板（先取回内容正文）。 */
async function openLayout(row: QueueItem) {
  try {
    const c = await getContent(row.content_id)
    layoutContent.value = c
    // 已发布 = 任务完成，面板转为只读「排版详情」
    layoutReadonly.value = row.status === 'published'
    layoutShow.value = true
  } catch (e) {
    message.error(`加载内容失败：${(e as Error).message}`)
  }
}

function onLayoutDone() {
  // 排版可能把状态从「待加工」推到「待发布」，刷新列表
  void queue.refresh()
}

async function openCheck(row: QueueItem) {
  checkItem.value = row
  const cfg = CHECKLISTS[row.platform]
  const saved = row.checklist ?? {}
  checks.value = Object.fromEntries(cfg.items.map((i) => [i.key, saved[i.key] ?? false]))
  checkVisible.value = true
}

async function saveCheck() {
  if (!checkItem.value) return
  await queue.setChecklist(checkItem.value.id, checks.value)
  checkVisible.value = false
  message.success('检查进度已保存')
}

async function markPublished(row: QueueItem) {
  await queue.setStatus(row.id, 'published')
  message.success('已标记为发布完成')
}

async function onDelete(row: QueueItem) {
  dialog.warning({
    title: '删除队列项',
    content: `确定删除「${row.title}」？此操作不可撤销。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      await queue.remove(row.id)
      message.success('已删除')
    },
  })
}

/**
 * 取消加工中状态（解卡）。
 *
 * 排版/生图任务因进程重启、异常或中途关页面而卡住时，Content.processing 会残留在
 * 后端，导致该行所有按钮被永久禁用。这个操作让后端清掉标记并停止前台等待。
 */
async function onCancelProcessing(row: QueueItem) {
  try {
    await cancelProcessing(row.content_id)
    message.success('已取消加工，可重新操作')
    await queue.refresh()
  } catch (e) {
    message.error(`取消失败：${(e as Error).message}`)
  }
}

/** 公众号推草稿箱 / 其他平台复制文案 */
async function onPushDraft(row: QueueItem) {
  // 正文不在队列项里，得回到内容表取一次
  let content: ContentItem | null = null
  try {
    content = await getContent(row.content_id)
  } catch {
    content = null
  }

  if (row.platform !== 'gzh') {
    // 非公众号：复制纯文本，用户在 App 里粘贴
    const body = content?.body ?? ''
    const ok = await navigator.clipboard
      .writeText(`${row.title}\n\n${body}`)
      .then(() => true)
      .catch(() => false)
    message[ok ? 'success' : 'warning'](
      ok ? '标题 + 正文已复制，去对应 App 粘贴发布' : '复制失败，请手动到内容详情复制',
    )
    return
  }

  // 公众号：必须用排版后的 HTML（未排版则引导去加工）
  const html = content?.body_html ?? ''
  if (!html) {
    message.warning('这条还没排版，先点「加工排版」生成公众号正文再推送')
    if (content) openLayoutFromContent(content)
    return
  }
  if (pushGate.blocked.value) {
    message.warning(pushGate.reason.value)
    return
  }

  pushing.value = true
  try {
    const res = await confirmTool({
      // 队列页推送不来自任何对话，不传 conversation_id
      tool_call_id: `q_${row.id}`,
      tool_name: 'push_wechat_draft',
      // 带 content_id：后端会把正文里的本地配图转存到微信，并用首图当封面
      arguments: { title: row.title, html, content_id: row.content_id },
    })
    const result = res.result as { ok?: boolean; error?: string; message?: string }
    if (result?.ok) {
      message.success(result.message ?? '已推送到草稿箱')
      auth.markUsed('wechat_push')
      await queue.refresh()
    } else {
      // 失败不置灰：后端对这一种有 refund（唯一同步可判成败的受控动作）
      message.error(result?.error ?? '推送失败')
    }
  } catch (e) {
    message.error(`推送失败：${(e as Error).message}`)
  } finally {
    pushing.value = false
  }
}

/** 直接带着已取到的内容打开加工面板，省一次请求。 */
function openLayoutFromContent(c: ContentItem) {
  layoutContent.value = c
  // 这条路径的语义是「还没排版，去排版」，恒为可编辑
  layoutReadonly.value = false
  layoutShow.value = true
}

async function onExport() {
  const md = await queue.exportMarkdown()
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `发布清单_${new Date().toISOString().slice(0, 10)}.md`
  a.click()
  URL.revokeObjectURL(a.href)
  message.success('清单已导出')
}

// ---- 新建内容：把手里已有的完整文案直接粘进入队 ----
// 场景：用户在别处（Word / 备忘录 / AI 聊天）已经写好了完整文案，
// 不需要再走对话创作，直接粘进来 → 队列按「待加工」起步，
// 之后照样能「加工排版」选主题生成公众号排版、推草稿箱。
const createShow = ref(false)
const createSaving = ref(false)
const createForm = ref({ platform: 'gzh', title: '', body: '' })

const createPlatformOptions = [
  { label: '公众号', value: 'gzh' },
  { label: '小红书', value: 'xiaohongshu' },
  { label: '抖音', value: 'douyin' },
]

function openCreate() {
  createForm.value = { platform: 'gzh', title: '', body: '' }
  createShow.value = true
}

async function onCreate() {
  const title = createForm.value.title.trim()
  const body = createForm.value.body.trim()
  if (!title) {
    message.warning('请填写标题')
    return
  }
  if (!body) {
    message.warning('请粘贴正文内容')
    return
  }
  createSaving.value = true
  try {
    await saveToQueue({ platform: createForm.value.platform, title, body })
    message.success('已入队：在行内点「加工排版」即可生成公众号排版')
    createShow.value = false
    await queue.refresh()
  } catch (e) {
    message.error(`入队失败：${(e as Error).message}`)
  } finally {
    createSaving.value = false
  }
}

onMounted(async () => {
  await queue.refresh()
  // 从对话页「去队列查看」跳来：高亮目标行，2.5 秒后自动淡出
  const cid = route.query.content_id as string | undefined
  if (!cid) return
  highlightId.value = cid
  await nextTick()
  document
    .querySelector(`tr[data-cid="${cid}"]`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  setTimeout(() => {
    if (highlightId.value === cid) highlightId.value = ''
  }, 2500)

  // 「返回对话」闭环条：优先用 query 带的来源会话，否则从该行自身的来源取
  const fromConv = route.query.from_conversation_id as string | undefined
  const fromMsg = route.query.from_msg_id as string | undefined
  const hit = queue.items.find((it) => it.content_id === cid)
  returnConvId.value = fromConv || hit?.conversation_id || ''
  returnMsgId.value = fromMsg || hit?.msg_id || ''
})

/** 目标行高亮 class（NDataTable 逐行调用）。 */
function rowClassName(row: QueueItem): string {
  const target = (row as QueueItem & { content_id?: string }).content_id
  return target === highlightId.value ? 'row-highlight' : ''
}

/** 给每行挂 data-cid，便于 scrollIntoView 精确定位到目标行。 */
function rowProps(row: QueueItem): Record<string, string> {
  const cid = (row as QueueItem & { content_id?: string }).content_id ?? ''
  return { 'data-cid': cid }
}

// ---- 加工中自动轮询：有任意一条在当前页处于排版/生图，就每 3s 定向刷新一次 ----
// 只重拉「当前页 + 处理中标记」，不重载统计、不整表替换；组件卸载/无加工任务即停。
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function syncPolling() {
  const busy = queue.items.some((i) => i.processing)
  if (busy && !pollTimer) {
    pollTimer = setInterval(() => void queue.refreshProcessingRows(), 3000)
  } else if (!busy && pollTimer) {
    stopPolling()
  }
}

watch(() => queue.items, syncPolling, { deep: true })
// 加工面板一关就不再轮询（面板自己会负责后端取消）
watch(layoutShow, (v) => {
  if (!v) syncPolling()
})
onBeforeUnmount(stopPolling)

/** 状态多选筛选：空数组 = 全部；与关键词搜索是 AND 关系。 */
const statusOptions: { label: string; value: QueueStatus }[] = [
  { label: '待加工', value: 'draft' },
  { label: '待发布', value: 'ready' },
  { label: '已发布', value: 'published' },
  { label: '已归档', value: 'archived' },
]

/** 标题/正文模糊搜索：本地输入做 300ms 防抖，再交给 store（后端匹配）。 */
const searchKw = ref('')
let searchTimer: ReturnType<typeof setTimeout> | undefined
function onSearchInput(v: string) {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void queue.setKeyword(v), 300)
}
function onSearchClear() {
  if (searchTimer) clearTimeout(searchTimer)
  searchKw.value = ''
  void queue.setKeyword('')
}
</script>

<template>
  <div>
    <NAlert type="info" :bordered="false" style="margin-bottom: 16px">
      成品集中在这里。按平台检查项确认后到对应渠道发布，再回来标记完成。
      公众号支持推送到草稿箱（官方接口，合规）；小红书和抖音无公开发布接口，需手动发布。
    </NAlert>

    <!-- 从对话页跳来：给出「返回对话」入口，形成 会话 ⇄ 队列 闭环 -->
    <NAlert
      v-if="returnConvId"
      type="success"
      :bordered="false"
      style="margin-bottom: 16px"
      closable
      @close="dismissReturnBar"
    >
      <div class="queue-return-bar">
        <span>已定位到你从对话里保存的那条内容</span>
        <IconTextBtn name="arrow-left" size="small" type="primary" ghost @click="goChatFromBar">
          返回对话
        </IconTextBtn>
      </div>
    </NAlert>

    <NCard size="small">
      <template #header>
        <div class="queue-header">
          <div class="queue-header-left">
            <NSelect
              multiple
              collapse-tags
              :value="queue.filterStatuses"
              :options="statusOptions"
              size="small"
              placeholder="按状态筛选（可多选）"
              style="width: 200px"
              @update:value="(v: QueueStatus[]) => queue.setStatuses(v)"
            />
            <NInput
              v-model:value="searchKw"
              size="small"
              clearable
              placeholder="搜索标题或正文"
              style="width: 200px"
              @input="onSearchInput"
              @clear="onSearchClear"
            >
              <template #prefix>
                <span style="font-size: 13px; opacity: 0.6">🔍</span>
              </template>
            </NInput>
          </div>
          <NSpace :size="8">
            <NTag size="small" :bordered="false">
              共 {{ queue.stats.total }} 条
            </NTag>
            <NTag size="small" type="warning" :bordered="false">
              待发布 {{ queue.stats.by_status?.ready ?? 0 }}
            </NTag>
            <IconTextBtn name="plus" size="small" type="primary" @click="openCreate">
              新建内容
            </IconTextBtn>
            <IconTextBtn name="download" size="small" @click="onExport">导出清单</IconTextBtn>
            <IconTextBtn name="reset" size="small" @click="queue.refresh()">刷新</IconTextBtn>
          </NSpace>
        </div>
      </template>

      <NEmpty v-if="!queue.items.length" description="暂无内容，去对话创作页生成" />
      <template v-else>
        <NDataTable
          :columns="columns"
          :data="queue.items"
          :loading="queue.loading"
          :row-key="(r: QueueItem) => r.id"
          :row-class-name="rowClassName"
          :row-props="rowProps"
          :scroll-x="1000"
          size="small"
        />
        <div class="queue-pager">
          <NPagination
            :page="queue.page"
            :page-size="queue.pageSize"
            :item-count="queue.total"
            :page-sizes="[10, 20, 50, 100]"
            show-size-picker
            :page-slot="7"
            @update:page="queue.setPage"
            @update:page-size="queue.setPageSize"
          />
        </div>
      </template>
    </NCard>

    <!-- 发布检查弹窗 -->
    <NModal
      v-model:show="checkVisible"
      preset="card"
      :title="checkItem ? `发布检查 · ${checkItem.title}${checkReadonly ? '（已完成，只读）' : ''}` : '发布检查'"
      style="max-width: 560px"
    >
      <template v-if="checkItem">
        <NAlert type="info" :bordered="false" style="margin-bottom: 14px">
          <div style="font-size: 12.5px; line-height: 1.7">
            <b>发布渠道：</b>{{ CHECKLISTS[checkItem.platform].channel }}<br />
            <b>操作路径：</b>{{ CHECKLISTS[checkItem.platform].path }}
          </div>
        </NAlert>

        <div v-for="item in CHECKLISTS[checkItem.platform].items" :key="item.key" class="check-row">
          <NCheckbox v-model:checked="checks[item.key]" :disabled="checkReadonly">
            {{ item.text }}
          </NCheckbox>
        </div>
      </template>

      <template #footer>
        <NSpace justify="end">
          <NButton @click="checkVisible = false">{{ checkReadonly ? '关闭' : '取消' }}</NButton>
          <IconTextBtn
            v-if="!checkReadonly"
            name="check"
            type="primary"
            :loading="pushing"
            @click="saveCheck"
          >保存进度</IconTextBtn>
        </NSpace>
      </template>
    </NModal>

    <!-- 新建内容：粘贴已有文案直接入队 -->
    <NModal
      v-model:show="createShow"
      preset="card"
      title="新建内容 · 粘贴已有文案"
      style="max-width: 680px"
    >
      <NAlert type="info" :bordered="false" style="margin-bottom: 14px">
        <div style="font-size: 12.5px; line-height: 1.7">
          适合<b>已经写好的完整文案</b>：粘进来进入「发布队列」，之后在行内点
          <b>「加工排版」</b>选主题生成公众号排版，再<b>「推微信草稿箱」</b>。
          正文支持 Markdown（# 标题、**加粗**、- 列表、> 引用都会保留）。
        </div>
      </NAlert>

      <div class="create-form">
        <div class="create-row">
          <span class="create-label">平台</span>
          <NSelect
            v-model:value="createForm.platform"
            :options="createPlatformOptions"
            size="small"
            style="width: 160px"
          />
        </div>
        <div class="create-row">
          <span class="create-label">标题</span>
          <NInput
            v-model:value="createForm.title"
            size="small"
            placeholder="给这条内容起个标题（公众号标题前 12 字要有钩子）"
            maxlength="64"
            show-count
          />
        </div>
        <div class="create-row create-row-body">
          <span class="create-label">正文</span>
          <NInput
            v-model:value="createForm.body"
            type="textarea"
            :rows="14"
            placeholder="把完整文案粘贴到这里，支持 Markdown…"
          />
        </div>
      </div>

      <template #footer>
        <NSpace justify="end">
          <NButton @click="createShow = false">取消</NButton>
          <IconTextBtn name="check" type="primary" :loading="createSaving" @click="onCreate">
            入队
          </IconTextBtn>
        </NSpace>
      </template>
    </NModal>

    <!-- 加工排版面板（已发布时只读，标题为「排版详情」） -->
    <LayoutPanel
      v-model:show="layoutShow"
      :content="layoutContent"
      :readonly="layoutReadonly"
      @done="onLayoutDone"
    />
  </div>
</template>

<style scoped>
/* 「返回对话」闭环条：文案左、按钮右 */
.queue-return-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12.5px;
}

.queue-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.queue-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* 新建内容弹窗：标签定宽 + 控件撑满 */
.create-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.create-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.create-row-body {
  align-items: flex-start;
}
.create-label {
  flex-shrink: 0;
  width: 44px;
  font-size: 13px;
  color: var(--app-text-2);
}
.create-row-body .create-label {
  padding-top: 4px;
}

.check-row {
  padding: 8px 0;
  border-bottom: 1px solid var(--app-border-60);
  font-size: 13px;
}

.check-row:last-child {
  border-bottom: none;
}

.queue-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

/* 从对话页跳转定位时的目标行高亮（NDataTable 行由渲染函数产出，需 :deep） */
:deep(tr.row-highlight td) {
  background: var(--app-primary-a16) !important;
  transition: background 0.4s ease;
}
:deep(tr.row-highlight td:first-child) {
  box-shadow: inset 3px 0 0 var(--app-primary);
}

@media (max-width: 768px) {
  .queue-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
