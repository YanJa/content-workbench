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
import { getContent, renameQueueItem, saveToQueue } from '@/api/queue'
import { createKnowledge, originCheck } from '@/api/knowledge'
import type { QueueItem, QueueStatus, ContentItem } from '@/types'
import LayoutPanel from '@/components/LayoutPanel.vue'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'
import { copyHtml, copyText } from '@/utils/clipboard'

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
/** 「存入知识库」的门控：权限同后端 POST /knowledge/items 的 knowledge.create */
const kbGate = usePermGate({ perm: 'knowledge.create' })
/** 正在存入知识库的 content_id（行级 loading，防连点重存） */
const savingKbId = ref('')

/**
 * 把这篇成稿存进知识库。后端取的是 Content.body（Markdown 正文），
 * 不是排版后的 HTML —— 内联样式对检索毫无价值，还会污染上下文。
 */
async function onSaveKnowledge(row: QueueItem) {
  savingKbId.value = row.content_id
  try {
    const { already_exists } = await createKnowledge({ content_id: row.content_id })
    if (already_exists) {
      message.info('这篇成稿已在知识库，无需重复存入')
    } else {
      message.success('已存入知识库，之后写稿时会自动参考')
    }
    // 刷新映射：该行立刻从「存入」翻成「已存入 / 查看知识库」
    await refreshStoredMap()
  } catch (e) {
    message.error((e as Error).message || '存入知识库失败')
  } finally {
    savingKbId.value = ''
  }
}

/**
 * content_id -> 知识库素材 id。标出「哪些成稿已经存进知识库」，
 * 已存的行把「存入知识库」替换成「已存入 / 查看知识库」，避免同一条任务反复存。
 * 知识库不可用也不阻塞队列 —— 拿不到映射就当都没存。
 */
const storedMap = ref<Record<string, string>>({})

async function refreshStoredMap() {
  console.log('[KBDEBUG] refreshStoredMap items=', queue.items.length)
  const ids = queue.items.map((i) => i.content_id).filter(Boolean)
  if (!ids.length) {
    storedMap.value = {}
    return
  }
  try {
    storedMap.value = await originCheck(ids)
    console.log('[KBDEBUG] storedMap=', JSON.stringify(storedMap.value))
  } catch (e) {
    console.log('[KBDEBUG] fail', (e as Error).message)
    storedMap.value = {}
  }
  console.log('[KBDEBUG] withKb count=', queueItemsWithKb.value.filter((x) => x.kbStored).length)
}

watch(
  () => queue.items,
  () => void refreshStoredMap(),
  { immediate: true },
)

/**
 * 把「已存进知识库」的状态并进每一行，作为 :data 传给表格。
 * 关键：NDataTable 会按行数据记忆化单元格，如果只在 render 闭包里读
 * storedMap，storedMap 变了表格也不重渲染。把状态并进 data，data 引用
 * 随 storedMap 变 → 表格自然重渲染，行按钮才会翻成「已存入 / 查看知识库」。
 */
const queueItemsWithKb = computed(() =>
  queue.items.map((i) => ({ ...i, kbStored: storedMap.value[i.content_id] ?? '' })),
)

/** 已存的成稿跳到知识库详情（?item= 由知识库页自动打开）。 */
function gotoKnowledge(itemId: string) {
  router.push({ path: '/knowledge', query: { item: itemId } })
}

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
/** 当前平台的检查项配置（平台没配过检查项时为 null，弹窗降级为纯确认） */
const checkCfg = computed(() => checklistOf(checkItem.value))

/**
 * 检查弹窗的两种来路：
 * - view：点「发布检查」进来，只勾进度、保存；
 * - publish：点「标记为已发布」进来，勾完之后还要走一次「确实已发了吗」的确认。
 */
const checkMode = ref<'view' | 'publish'>('view')
const pushing = ref(false)
/** 正在删微信草稿的 content_id（行级 loading，防连点重复删） */
const deletingDraftId = ref('')

// 加工排版面板
const layoutShow = ref(false)
const layoutContent = ref<ContentItem | null>(null)
/** 已发布任务的排版面板为只读「排版详情」（由队列状态驱动，见 LayoutPanel 的 readonly） */
const layoutReadonly = ref(false)

const statusMeta: Record<QueueStatus, { label: string; type: 'default' | 'warning' | 'success' | 'info' | 'error' }> = {
  draft: { label: '待加工', type: 'default' },
  ready: { label: '待发布', type: 'warning' },
  published: { label: '已发布', type: 'success' },
  archived: { label: '已归档', type: 'info' },
  abandoned: { label: '已放弃', type: 'error' },
}

const platformName: Record<string, string> = {
  gzh: '📰 公众号',
  xiaohongshu: '📕 小红书',
  douyin: '🎵 抖音',
  zhihu: '✍️ 知乎',
}

/**
 * 子形态展示名（与后端 ZHIHU_SUBTYPE_LABELS / GZH_SUBTYPE_LABELS 同一套口径）。
 * 两种平台的子形态都是「同一平台、不同发布链路」：光写平台名用户不知道该往哪儿发。
 */
const SUB_NAME: Record<string, Record<string, string>> = {
  zhihu: { answer: '回答', article: '文章', idea: '想法' },
  gzh: { article: '图文', tietu: '贴图' },
}

/** 加工中标记 → 展示文案（'' 表示空闲） */
const PROCESSING_LABEL: Record<string, string> = {
  layout: '排版中',
  image: '生图中',
}

/** 各平台的发布路径与检查项（与后端 buildPublishChecklist 对应） */
const CHECKLISTS: Record<string, { channel: string; path: string; items: { key: string; text: string }[] }> = {
  // 公众号默认形态「图文」走富文本排版 + 草稿箱；
  // 另一种形态「贴图」见文末 gzh_tietu（图片消息，链路完全不同）。
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
  // 知乎三种形态是三条不同的发布路径，检查项也完全不同：
  // 回答看「增量」，文章看「结构」，想法看「话题标签」。
  zhihu_answer: {
    channel: '知乎问题页（手动发布）',
    path: '复制富文本 → 到问题页点「写回答」→ 粘贴 → 发布 → 回来标记已发',
    items: [
      { key: 'question', text: '归属问题已确认（回答挂对了地方）' },
      { key: 'lead', text: '开头两屏内有明确结论' },
      { key: 'increment', text: '写清楚了「比现有高赞回答多了什么」' },
      { key: 'support', text: '每个分点都有数据 / 案例 / 操作步骤其中之一' },
      { key: 'tone', text: '没有求赞收藏的套话、没有八股连接词' },
      { key: 'paste', text: '粘贴后小标题与列表格式没乱' },
    ],
  },
  zhihu_article: {
    channel: '知乎专栏（手动发布）',
    path: '复制富文本 → 到专栏写作页 → 粘贴 → 加话题 → 发布 → 回来标记已发',
    items: [
      { key: 'title', text: '标题 15-30 字，不是标题党' },
      { key: 'digest', text: '摘要已填（100 字内）' },
      { key: 'structure', text: '分 4-6 节，节与节之间是推进关系' },
      { key: 'actionable', text: '至少有一处可复现的清单 / 对比 / 步骤' },
      { key: 'topic', text: '已添加话题（影响分发）' },
      { key: 'paste', text: '粘贴后小标题与列表格式没乱' },
    ],
  },
  zhihu_idea: {
    channel: '知乎想法（手动发布）',
    path: '复制文案 → 到创作中心发想法 → 粘贴 → 加话题 → 发布 → 回来标记已发',
    items: [
      { key: 'one', text: '只讲了一个观点，没有展开成文章' },
      { key: 'lead', text: '第一行就有信息量' },
      { key: 'len', text: '50-300 字，没写成长文' },
      { key: 'tags', text: '话题标签 1-3 个已带' },
    ],
  },
  // 公众号「贴图」是图片消息：没有富文本排版、没有草稿箱接口，
  // 发布链路是「图片 + 一段纯文本」，检查项跟图文文章完全不同。
  gzh_tietu: {
    channel: '公众号图片消息（手动发布）',
    path: '复制文案 → 导出配图 3-9 张 → 到公众号后台发「图片」→ 回来标记已发',
    items: [
      { key: 'title', text: '首图标题 ≤20 字，前 8 个字出钩子' },
      { key: 'imgs', text: '配图 3-9 张已导出，首图信息量最大' },
      { key: 'len', text: '正文 100-400 字，只讲一个点' },
      { key: 'tone', text: '口语第一人称，没有「希望对你有帮助」式收尾' },
      { key: 'tags', text: '话题标签最多 1 个，没堆一排' },
      { key: 'paste', text: '文案已粘进图片消息的文本区' },
    ],
  },
}

/**
 * 检查项配置：知乎 / 公众号按子形态取（各自形态路径不同），其余平台按 platform 取。
 */
function checklistOf(row: QueueItem | null) {
  if (!row) return null
  if (row.platform === 'zhihu') {
    return CHECKLISTS[`zhihu_${row.sub_type || 'answer'}`] ?? CHECKLISTS.zhihu_answer
  }
  if (row.platform === 'gzh' && (row.sub_type || 'article') === 'tietu') {
    return CHECKLISTS.gzh_tietu
  }
  return CHECKLISTS[row.platform] ?? null
}

const columns = computed<DataTableColumns<QueueItem>>(() => [
  {
    title: '标题',
    key: 'title',
    ellipsis: { tooltip: true },
    render: (row) =>
      h(NSpace, { size: 4, wrap: false, align: 'center' }, () => [
        h(
          'span',
          {
            title: row.title,
            style:
              'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:260px',
          },
          row.title,
        ),
        // 未排版之前标题还能改 —— 能不能改由后端说了算（title_editable），
        // 前端不自己推断，免得按钮在、点下去 409。
        row.title_editable
          ? h(NButton, {
              size: 'tiny',
              quaternary: true,
              title: '改标题（排版之后就不能改了）',
              onClick: () => openRename(row),
            }, { default: () => '改' })
          : null,
      ]),
  },
  {
    title: '平台',
    key: 'platform',
    width: 130,
    // 知乎 / 公众号带上子形态：回答/文章/想法、图文/贴图都是完全不同的发布路径，
    // 光写平台名用户不知道这条该往哪儿发。
    render: (row) => {
      const base = platformName[row.platform] ?? row.platform
      const subTable = SUB_NAME[row.platform]
      if (!subTable) return base
      const sub = subTable[row.sub_type ?? ''] ?? Object.values(subTable)[0]
      return h('span', null, [
        base,
        h('span', { style: 'color:var(--app-text-3);font-size:12px' }, `·${sub}`),
      ])
    },
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
    width: 360,
    render: (row) => {
      // 加工中（排版/生图）禁用所有会改状态的操作，避免重复触发/冲突
      const busy = !!row.processing
      // 已放弃：不发了，发布链路上的所有操作都停掉，只保留「恢复」与「删除」等管理项
      const isAbandoned = row.status === 'abandoned'
      // 贴图是公众号的图片消息形态：没有草稿箱接口，复制的是纯文本，
      // 所以发布链路上的「推送 / 标记已发」门槛与图文文章不同。
      const isTietu = row.platform === 'gzh' && (row.sub_type ?? 'article') === 'tietu'
      const isGzh = row.platform === 'gzh' && !isTietu
      const isZhihu = row.platform === 'zhihu'
      const items = [
        // 已放弃态：提供「恢复加工」把任务拉回待加工，撤销放弃
        isAbandoned
          ? actionBtn('refresh', '恢复加工（撤销放弃，回到待加工）', () => onRestore(row), {
              type: 'default',
            })
          : null,
        // 加工排版 / 查看排版：加工中时禁用，tooltip 显示当前加工阶段
        actionBtn(
          'edit',
          busy
            ? `加工中：${PROCESSING_LABEL[row.processing!] ?? '排版中'}`
            : row.status === 'published'
              ? '查看排版'
              : '加工排版',
          () => openLayout(row),
          { type: row.status === 'published' ? 'default' : 'primary', disabled: busy || isAbandoned },
        ),
        // 发布检查
        actionBtn('search', '发布检查', () => openCheck(row), { disabled: busy || isAbandoned }),
        // 推微信草稿箱（公众号）/ 复制文案（其它平台）：额度耗尽时禁用并说明。
        // pushing = 有一条推送请求正在飞：此时禁用所有行的推送按钮，防止
        // 用户连点同一条记录造成二次推送（后端回填 media_id 有回程延迟）。
        //
        // 已推送（media_id 在）时推送按钮让位给「删除微信草稿」：
        // 推错了 / 想改完再推，删掉微信侧草稿后本地状态回退、按钮回来。
        // 微信删除不可恢复，dialog 二次确认里会写明。
        row.wechat_media_id
          ? actionBtn(
              'draft-trash',
              '删除微信草稿箱里的这篇草稿（微信侧不可恢复，删除后可重新推送）',
              () => onDeleteDraft(row),
              { type: 'warning', loading: deletingDraftId.value === row.content_id },
            )
          : isGzh && pushGate.blocked.value
            ? actionBtn('send', '无法推送：今日公众号推送额度已用完', () => onPushDraft(row), {
                type: 'primary',
                disabled: true,
              })
            : actionBtn(
                isGzh ? 'send' : 'copy',
                isGzh
                  ? `推微信草稿箱${pushGate.left.value === null ? '' : `（剩 ${pushGate.left.value}）`}`
                  : isZhihu
                    ? '复制富文本（粘到知乎编辑器保留标题与列表）'
                    : isTietu
                      ? '复制贴图文案（纯文本，粘进图片消息）'
                      : '复制文案',
                () => onPushDraft(row),
                {
                  type: isGzh ? 'primary' : 'default',
                  // 注意：这里是 script 作用域，ref 不会自动解包，必须 .value
                  disabled: busy || pushing.value || (isGzh && pushGate.blocked.value) || isAbandoned,
                },
              ),
        // 知乎：官方没有发布接口，系统给的是「人带着内容去发」的入口。
        // 这个按钮省掉的是「我该去哪个页面发」这一步 —— 三种形态入口各不相同。
        isZhihu && row.publish_url && !isAbandoned
          ? actionBtn('link', '去知乎发布（打开对应形态的发布页）', () => gotoPublish(row), {
              type: 'primary',
              disabled: busy,
            })
          : null,
        // 标记已发：图文必须先推到微信草稿箱（wechat_media_id 非空）才能标记，
        // 否则推完草稿箱前就标记完成，等于绕过合规发布流程。
        // 贴图没有草稿箱接口，复制文案即可去发布，不做此门槛。
        // 已放弃的任务不再能标记已发（先恢复才能继续发布流程）。
        row.status !== 'published' && !isAbandoned
          ? actionBtn(
              'check-circle',
              isGzh && !row.wechat_media_id
                ? '请先推送到微信草稿箱，再标记完成'
                : isTietu
                  ? '标记为已发布（复制文案去公众号后台发图片消息）'
                  : '标记为已发布（先过一遍发布检查）',
              () => openPublishCheck(row),
              { disabled: busy || (isGzh && !row.wechat_media_id) },
            )
          : null,
        // 存入知识库：把这篇成稿变成「AI 写下一篇时的参考素材」。
        // 已存过的行（kbStored 命中）不再显示「存入」，换成「已存入 / 查看」，
        // 避免同一条任务被反复存进知识库。
        row.kbStored
          ? [
              actionBtn('book', '已在知识库（写稿时 AI 自动参考）', () => {}, { disabled: true }),
              actionBtn(
                'link',
                '查看知识库',
                () => gotoKnowledge(row.kbStored!),
                { disabled: busy },
              ),
            ]
          : kbGate.blocked.value
            ? actionBtn('book', '没有存入知识库的权限，请联系管理员', () => {}, { disabled: true })
            : actionBtn('book', '存入知识库（写稿时 AI 自动参考）', () => onSaveKnowledge(row), {
                loading: savingKbId.value === row.content_id,
              }),
        // 放弃：把任务移出发布流程（内容保留，可随时「恢复加工」撤销）。已放弃的不显示。
        !isAbandoned
          ? actionBtn(
              'close-circle',
              '放弃这条内容（移出发布流程，可随时恢复加工）',
              () => onAbandon(row),
              { type: 'warning' },
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

function openCheck(row: QueueItem) {
  checkMode.value = 'view'
  _fillCheck(row)
}

/**
 * 点「标记为已发布」的第一步：先过一遍发布检查。
 *
 * 已推送草稿箱 ≠ 已发布 —— 中间还有「去微信后台核对并群发」这一步是系统
 * 管不到的。所以标记完成前把检查项摆出来让人对一遍，勾完再确认一次。
 * 检查项**不强制全勾**（只是提示，不是闸门），没勾完也能继续。
 */
function openPublishCheck(row: QueueItem) {
  checkMode.value = 'publish'
  _fillCheck(row)
}

function _fillCheck(row: QueueItem) {
  const cfg = checklistOf(row)
  const saved = row.checklist ?? {}
  checkItem.value = row
  checks.value = Object.fromEntries(
    (cfg?.items ?? []).map((i) => [i.key, saved[i.key] ?? false]),
  )
  checkVisible.value = true
}

async function saveCheck() {
  if (!checkItem.value) return
  await queue.setChecklist(checkItem.value.id, checks.value)
  checkVisible.value = false
  message.success('检查进度已保存')
}

/** 未勾选的检查项条数（提示用，不阻断） */
function uncheckedCount(row: QueueItem): number {
  const cfg = checklistOf(row)
  if (!cfg) return 0
  return cfg.items.filter((i) => !checks.value[i.key]).length
}

/**
 * 发布检查里的「下一步」：存下勾选进度，再弹确认框问一次。
 *
 * 两道闸的意义不同：检查是「该做的事都做了吗」（提示，可不全勾），
 * 确认是「真的已经在渠道上发出去了吗」（结论，点了才落库）。
 */
async function onPublishCheckNext() {
  const row = checkItem.value
  if (!row) return
  // 勾选进度先存：即使下面确认框点了取消，勾选也不白勾
  try {
    await queue.setChecklist(row.id, checks.value)
  } catch (e) {
    message.warning(`检查进度没保存上：${(e as Error).message}`)
  }
  checkVisible.value = false

  const left = uncheckedCount(row)
  dialog.warning({
    title: '确认已经发布了吗？',
    // 用 VNode 而不是拼字符串：dialog 的 content 不保证按 \n 换行，
    // 拼出来的长句挤成一坨就没有「提示」的作用了。
    content: () =>
      h('div', { style: 'line-height:1.7' }, [
        h('p', { style: 'margin:0 0 8px' }, [
          '「',
          h('b', null, row.title),
          '」将被标记为已发布，并从待发布列表移出。',
        ]),
        left > 0
          ? h(
              'p',
              { style: 'margin:0 0 8px;color:var(--app-text-2)' },
              `发布检查还有 ${left} 项没勾 —— 不影响继续，只是提醒。`,
            )
          : h('p', { style: 'margin:0 0 8px' }, '发布检查已全部确认。'),
        h(
          'p',
          { style: 'margin:0;color:var(--app-text-2)' },
          '只有你确实已经在对应渠道点过发布才点确认：标记错了能改回来，但统计会失真。',
        ),
      ]),
    positiveText: '确认已发布',
    negativeText: '还没有',
    onPositiveClick: async () => {
      await queue.setStatus(row.id, 'published')
      message.success('已标记为发布完成')
    },
  })
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

// ------------------------------------------------------------------ 放弃 / 恢复
/**
 * 放弃：把任务移出发布流程。内容本身保留（不删数据），只是不再走发布链路。
 * 放弃后发布相关操作全部禁用，只留「恢复加工」拉回待加工。可逆。
 */
function onAbandon(row: QueueItem) {
  dialog.warning({
    title: '放弃这条内容？',
    content: `「${row.title}」将标记为「已放弃」，移出发布流程。内容保留，可随时点「恢复加工」撤销。`,
    positiveText: '放弃',
    negativeText: '取消',
    onPositiveClick: async () => {
      await queue.setStatus(row.id, 'abandoned')
      message.success('已放弃，内容保留')
    },
  })
}

/** 恢复：把已放弃的任务拉回「待加工」，重新进入发布流程。 */
async function onRestore(row: QueueItem) {
  await queue.setStatus(row.id, 'draft')
  message.success('已恢复，回到待加工')
}

// ------------------------------------------------------------------ 改标题
const renameShow = ref(false)
const renameRow = ref<QueueItem | null>(null)
const renameDraft = ref('')
const renameSaving = ref(false)

function openRename(row: QueueItem) {
  renameRow.value = row
  renameDraft.value = row.title
  renameShow.value = true
}

async function onSaveRename() {
  const row = renameRow.value
  if (!row) return
  const title = renameDraft.value.trim()
  if (!title) {
    message.warning('标题不能为空')
    return
  }
  if (title === row.title) {
    renameShow.value = false
    return
  }
  renameSaving.value = true
  try {
    await renameQueueItem(row.id, title)
    message.success('标题已更新')
    renameShow.value = false
    await queue.refresh()
  } catch (e) {
    // 409 = 这一刻刚好排版完成/推了草稿箱，不是 bug，刷新即可
    message.error((e as Error)?.message || '改标题失败')
    await queue.refresh()
  } finally {
    renameSaving.value = false
  }
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

/**
 * 「去知乎发布」：新窗口打开对应形态的发布页。
 *
 * 知乎官方没有发布接口（详见 docs/知乎内容生产调研.md），所以系统能做的
 * 是让人少走一步——不用自己去找「写文章/写回答/发想法」在哪。
 */
function gotoPublish(row: QueueItem) {
  if (!row.publish_url) return
  window.open(row.publish_url, '_blank', 'noopener,noreferrer')
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

  // 公众号贴图：图片消息没有草稿箱接口，复制的是规整后的纯文本
  if (row.platform === 'gzh' && (row.sub_type ?? 'article') === 'tietu') {
    const tietuText =
      (content?.layouts as Record<string, { text?: string }> | null)?.gzh?.text ||
      content?.body ||
      ''
    const ok = await copyText(tietuText)
    message[ok ? 'success' : 'warning'](
      ok ? '贴图文案已复制，配好 3-9 张图后去公众号后台发「图片」' : '复制失败，请到「加工排版」里手动复制',
    )
    return
  }

  if (row.platform !== 'gzh') {
    // 知乎：编辑器是富文本，粘 Markdown 源码会原样显示 # 和 *，
    // 所以有排版产物时复制成 HTML（保留标题/加粗/列表），没有才退化为纯文本。
    if (row.platform === 'zhihu') {
      const html = (content?.layouts as Record<string, { html?: string }> | null)?.zhihu?.html
        || content?.body_html
        || ''
      if (html) {
        const r = await copyHtml(html, `${row.title}\n\n${content?.body ?? ''}`)
        message[r === 'fail' ? 'error' : 'success'](
          r === 'rich'
            ? '富文本已复制，去知乎编辑器里直接粘贴（标题与列表格式保留）'
            : r === 'plain'
              ? '已复制为纯文本（当前浏览器不支持复制富文本）'
              : '复制失败，请到「加工排版」里手动复制',
        )
        return
      }
    }
    // 其它平台 / 知乎还没排版：复制纯文本，用户在 App 里粘贴
    const body = content?.body ?? ''
    const ok = await copyText(`${row.title}\n\n${body}`)
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

/**
 * 删除微信草稿箱里的草稿（推错了 / 想改完再推）。
 *
 * 微信侧删除不可恢复（草稿箱没有回收站），所以 dialog 二次确认里写清楚。
 * 成功后后端清掉该行的 wechat_media_id，列表刷新后推送按钮回来——
 * 本地状态与微信侧保持一致，可重新推送。
 */
async function onDeleteDraft(row: QueueItem) {
  if (!row.wechat_media_id) return
  dialog.warning({
    title: '删除微信草稿',
    content: `确定删除「${row.title}」在公众号草稿箱里的草稿？微信侧删除不可恢复，删除后本条记录恢复为可重新推送。`,
    positiveText: '删除草稿',
    negativeText: '取消',
    onPositiveClick: async () => {
      deletingDraftId.value = row.content_id
      try {
        const res = await confirmTool({
          tool_call_id: `qd_${row.id}`,
          tool_name: 'delete_wechat_draft',
          // 带 content_id：后端据此定位要清状态的队列记录
          arguments: { media_id: row.wechat_media_id!, content_id: row.content_id },
        })
        const result = res.result as {
          ok?: boolean
          error?: string
          errcode?: number
          message?: string
        }
        if (result?.ok) {
          message.success(result.message ?? '草稿已删除，可重新推送')
          await queue.refresh()
        } else if (result?.errcode === 40007) {
          // 微信侧草稿已不存在（用户在公众号后台删过 / 被清理）：本地「已推送」
          // 标记本就是陈旧信息，后端已一并清掉——按成功语义提示，刷新后推送按钮回来
          message.warning('微信侧草稿已不存在，本地记录已恢复为可推送')
          await queue.refresh()
        } else {
          message.error(result?.error ?? '删除失败')
        }
      } catch (e) {
        message.error(`删除失败：${(e as Error).message}`)
      } finally {
        deletingDraftId.value = ''
      }
    },
  })
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
  { label: '已放弃', value: 'abandoned' },
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
          :data="queueItemsWithKb"
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
      :title="
        checkItem
          ? `发布检查 · ${checkItem.title}${checkReadonly ? '（已完成，只读）' : ''}`
          : '发布检查'
      "
      style="width: 560px; max-width: 90vw"
    >
      <template v-if="checkItem">
        <NAlert
          v-if="checkMode === 'publish'"
          type="warning"
          :bordered="false"
          style="margin-bottom: 14px"
        >
          标记为已发布之前，先对一遍下面这些项。
          <b>不强制全勾</b> —— 只是提醒，没勾完也能继续。
        </NAlert>
        <NAlert v-if="checkCfg" type="info" :bordered="false" style="margin-bottom: 14px">
          <div style="font-size: 12.5px; line-height: 1.7">
            <b>发布渠道：</b>{{ checkCfg.channel }}<br />
            <b>操作路径：</b>{{ checkCfg.path }}
          </div>
        </NAlert>

        <div v-for="item in checkCfg?.items ?? []" :key="item.key" class="check-row">
          <NCheckbox v-model:checked="checks[item.key]" :disabled="checkReadonly">
            {{ item.text }}
          </NCheckbox>
        </div>
      </template>

      <template #footer>
        <NSpace justify="end">
          <NButton @click="checkVisible = false">{{ checkReadonly ? '关闭' : '取消' }}</NButton>
          <!-- 从「标记为已发布」进来：勾完走「下一步」，由确认框收口 -->
          <IconTextBtn
            v-if="checkMode === 'publish' && !checkReadonly"
            name="check-circle"
            type="primary"
            @click="onPublishCheckNext"
          >下一步</IconTextBtn>
          <IconTextBtn
            v-else-if="!checkReadonly"
            name="check"
            type="primary"
            :loading="pushing"
            @click="saveCheck"
          >保存进度</IconTextBtn>
        </NSpace>
      </template>
    </NModal>

    <!-- 改标题：只在排版之前能改 -->
    <NModal
      v-model:show="renameShow"
      preset="card"
      title="修改标题"
      style="width: 560px; max-width: 90vw"
    >
      <NAlert type="default" :bordered="false" style="margin-bottom: 14px">
        标题还没参与排版，现在改不影响任何产物。一旦排版（或推过草稿箱）就不能再改了
        —— 那时标题已经写进 HTML、封面和微信草稿，改了会对不上。
      </NAlert>
      <NInput
        v-model:value="renameDraft"
        placeholder="新标题"
        maxlength="64"
        show-count
        @keydown.enter="onSaveRename"
      />
      <template #footer>
        <NSpace justify="end">
          <NButton size="small" @click="renameShow = false">取消</NButton>
          <NButton size="small" type="primary" :loading="renameSaving" @click="onSaveRename">
            保存
          </NButton>
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
