<script setup lang="ts">
/**
 * 队列「加工排版」面板：把纯内容加工成可发布的成稿。
 *
 * 形态：可全屏的 Modal。左侧原文（Markdown），右侧选主题 → 一键排版 → 预览
 * （模拟公众号宽度，支持 手机/平板/桌面 三档）→ 查看合规校验 → 推草稿箱。
 * 配图在下方单独管理。
 *
 * 排版是**后台任务**（单次约 3 分钟），提交拿 task_id 后轮询进度，
 * 不再让前端请求干等到超时。
 */
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  NModal,
  NSelect,
  NCheckbox,
  NTag,
  NAlert,
  NDivider,
  NEmpty,
  NInput,
  NInputNumber,
  NProgress,
  NRadioGroup,
  NRadioButton,
  NUpload,
  NTabs,
  NTabPane,
  useDialog,
  useMessage,
} from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import MarkdownEditor from './MarkdownEditor.vue'
import IconTextBtn from './IconTextBtn.vue'
import HumanTracePanel from './HumanTracePanel.vue'
import MarkdownView from './MarkdownView.vue'
import ImageManager from './ImageManager.vue'
import {
  getThemeOptions,
  layoutContent,
  getLayoutJob,
  cancelProcessing,
  refreshContent,
  generateImages,
  uploadImage,
  getImageProviders,
  type ThemeOption,
} from '@/api/layout'
import { updateContent, updatePlaintext } from '@/api/queue'
import { confirmTool } from '@/api/tools'
import { getAuthorInfo, listWeChatAccounts, updateAuthorInfo } from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'
import { copyHtml, copyText } from '@/utils/clipboard'
import type { ContentItem, ContentImage, WeChatAccount } from '@/types'

const auth = useAuthStore()

/** 一键排版的门控：权限 layout.run + 配额 layout（异步任务，提交即预扣） */
const layoutGate = usePermGate({ perm: 'layout.run', quota: 'layout' })
/** 推送草稿的门控：权限 wechat.push + 配额 wechat_push（唯一同步可判成败的，失败会退还） */
const pushGate = usePermGate({ perm: 'wechat.push', quota: 'wechat_push' })

const props = defineProps<{
  show: boolean
  content: ContentItem | null
  /**
   * 只读（排版详情）：已发布的任务已经完成，不该被改。
   *
   * 由调用方按**队列状态**决定（`QueueView` 传 `row.status === 'published'`），
   * 而不是由内容自身决定 —— `ContentItem` 不含队列状态，且同一内容可能挂在
   * 多个队列项上，只有队列项知道「发布到哪一步了」。
   */
  readonly?: boolean
}>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'done'): void
}>()

const isReadonly = computed(() => !!props.readonly)

const message = useMessage()
const dialog = useDialog()
const themes = ref<ThemeOption[]>([])
const theme = ref('moyu-green')
const layouting = ref(false)
const pushing = ref(false)
const saving = ref(false)
const local = ref<ContentItem | null>(null)
const errors = ref<string[]>([])
const warnings = ref<string[]>([])
const fullscreen = ref(false)

// 编辑态：body（左栏 Markdown）/ body_html（右栏所见即所得）任一改动即置脏
const dirty = ref(false)

// 作者信息（排版时替换 {{作者名}} / {{一句话简介}}）
// 初值取系统设置里的默认；用户改了只影响这一篇（不落库），旁边有「存为默认」按钮
const authorName = ref('')
const authorIntro = ref('')
const authorTouched = ref(false)

// 右栏 iframe：编辑期只把 initialDoc 设一次，避免 srcdoc 响应式刷新导致光标跳回开头
const previewRef = ref<HTMLIFrameElement | null>(null)
const initialDoc = ref('')
const editedHtml = ref('')
/** 纯文本预览的 textarea 引用（autosize 用） */
const plainEl = ref<HTMLTextAreaElement | null>(null)
let inputTimer: ReturnType<typeof setTimeout> | null = null

// 占位符字面量（放常量里，避免在模板里写 `{{...}}` 被 Vue 当成插值解析）
const PH_NAME = '{{作者名}}'
const PH_INTRO = '{{一句话简介}}'

// 预览「点哪插哪」插入 / 点击已有图替换 的交互状态。
// 思路：预览 iframe 是 contenteditable，直接操作它的 DOM 把 <img> 插到点击位置 /
// 改 src，再把改动后的 innerHTML 回写 editedHtml 落库 —— 复用 `_inject_images`
// 产出的真实 <img> 形态，不依赖排版时的 <!--IMG:n--> 占位（那是 LLM 排版路径）。
const insertMode = ref(false)
const providers = ref<{ script: boolean; cloudflare: boolean }>({ script: false, cloudflare: false })
const pickerProvider = ref<'script' | 'cloudflare'>('script')
const genPrompt = ref('')
const genCount = ref(1)
const generating = ref(false)
const uploading = ref(false)
const picker = reactive({
  show: false,
  mode: 'insert' as 'insert' | 'replace',
  tab: 'gen' as 'gen' | 'upload' | 'existing',
  insertRef: null as HTMLElement | null,
  imgEl: null as HTMLImageElement | null,
})

// 公众号账号（多号时可选）；空 = 用后端默认号 / .env
const wechatAccounts = ref<WeChatAccount[]>([])
const accountId = ref('')

// 关注卡说明：排版时用哪个号、二维码配没配（只读提示，帮用户确认「点和关注关联的是谁」）
const followCardHint = computed(() => {
  const a = wechatAccounts.value.find((x) => x.id === accountId.value) ?? wechatAccounts.value[0]
  if (!a) return ''
  const qr = a.qrcode_set ? '已配二维码' : '未配二维码'
  return `关注卡：${a.gzh_name || a.name || a.appid}（${qr}）`
})

/**
 * 知乎内容：排版走「Markdown → 干净语义 HTML」那条路，不需要选主题
 * （主题是公众号内联样式那套的概念，知乎编辑器会把 style 剥掉）。
 */
const isZhihu = computed(() => local.value?.platform === 'zhihu')

/**
 * 公众号「贴图」子形态：图片消息没有富文本，排版产物是 layouts.gzh.text
 * 纯文本（首图标题 + emoji 分点），所以主题/话题标签/作者占位符这套
 * 图文专属链路不适用。
 *
 * 但「推草稿箱」是适用的：微信 draft/add、draft/update 都支持
 * article_type=newspic（图片消息），见 onPushDraft 里的分支。
 */
const isTietu = computed(
  () => local.value?.platform === 'gzh' && (local.value?.sub_type ?? 'article') === 'tietu',
)
/**
 * 纯文本平台：今日头条、百家号。
 *
 * 与「贴图」同一个道理 —— 发布端粘贴的是纯文本，没有富文本、没有主题、
 * 没有草稿箱接口。素材直接存在 layouts[platform].text。
 */
const PLAINTEXT_PLATFORMS = ['toutiao', 'baijiahao'] as const
/** 有 `layouts[platform].text` 这条产物的平台集合 */
type PlainPlatform = 'gzh' | 'xiaohongshu' | 'zhihu' | 'toutiao' | 'baijiahao'
const isPlaintextPlatform = computed(() =>
  (PLAINTEXT_PLATFORMS as readonly string[]).includes(local.value?.platform ?? ''),
)
/** 当前平台规整后的纯文本（贴图 / 头条 / 百家号共用这个读取口径） */
const plaintextText = computed(() => {
  const layouts = (local.value?.layouts ?? {}) as Record<string, { text?: string }>
  if (isTietu.value) return layouts.gzh?.text || ''
  return layouts[local.value?.platform ?? '']?.text || ''
})
/** 这段文本存在 layouts 的哪个键下：贴图复用 gzh，其余平台用自己的 key */
const plainKey = computed<PlainPlatform>(() =>
  isTietu.value ? 'gzh' : ((local.value?.platform ?? 'gzh') as PlainPlatform),
)

/**
 * 手改后的纯文本。`null` = 没动过，此时一律读排版产物。
 *
 * 用 null 而不是空串当「未编辑」：用户可能真的要把文案清空重写，
 * 空串是合法内容，不能和「没改」共用同一个值。
 */
const editedPlain = ref<string | null>(null)
const plainDirty = ref(false)
/** 正在展示/编辑的文本：改过用改过的，没改就用排版产物 */
const plainDraft = computed({
  get: () => editedPlain.value ?? plaintextText.value,
  set: (v: string) => {
    editedPlain.value = v
    plainDirty.value = v !== plaintextText.value
  },
})
/** 手改过且内容确实和产物不同 —— 用来显示「已手动编辑」和重置按钮 */
const hasPlainEdit = computed(
  () => editedPlain.value !== null && editedPlain.value !== plaintextText.value,
)
/** 任何一处（正文 / 富文本预览 / 纯文本预览）有未保存改动 */
const hasChanges = computed(() => dirty.value || plainDirty.value)
/**
 * 「不适用公众号那套富文本链路」的总开关：贴图 / 头条 / 百家号都算。
 * 主题、封面、作者占位符、话题标签的渲染条件统一用它，
 * 免得每加一个纯文本平台就要去改十处 v-if。
 *
 * 注意**推草稿箱不在内**：贴图虽然走纯文本，但微信 draft/add 支持
 * article_type=newspic，能推「图片消息」草稿。那个按钮改用 canPushDraft 单独门控。
 */
const isPlaintext = computed(() => isTietu.value || isPlaintextPlatform.value)

/**
 * 配图面板只给公众号两类（图文 / 贴图）显示。
 *
 * 发布链路里只有公众号有草稿箱 API：知乎只给发布页链接，小红书 / 抖音 /
 * 头条 / 百家号都是「复制文本带走」。配好的图在这些平台根本带不过去，
 * 留着入口只会让人误以为图会跟着稿子一起发布，属于半成品。
 *
 * 公众号图文用配图做正文插图和封面；贴图的图片消息本身就是「图 + 短文案」，
 * 配图是内容主体（推 newspic 草稿时逐张上传为永久素材，首图即封面）。
 * 所以按 platform === 'gzh' 一并保留（gzh 只有 article / tietu 两个子形态）。
 *
 * 只收起 UI 入口，不动 content.images：其它平台历史内容里已配过的图仍在库里，
 * 将来要恢复显示或做「打包下载」不用重做数据。
 */
const showImagePanel = computed(() => local.value?.platform === 'gzh')

/**
 * 推草稿箱按钮能否点。两类要求不同：
 *  - 图文 news：必须有排版后的 HTML（还要有封面，后端会再拦一次）
 *  - 贴图 newspic：必须有文案 + 至少 1 张图（图片消息首图即封面，没有封面字段）
 */
const tietuImages = computed(() => local.value?.images || [])
const canPushDraft = computed(() =>
  isTietu.value
    ? !!plainDraft.value.trim() && tietuImages.value.length > 0
    : hasHtml.value,
)
/** 推草稿按钮是否显示：公众号图文、公众号贴图；其余平台没有草稿箱 API */
const canShowPushDraft = computed(
  () => !isReadonly.value && (isTietu.value || (!isZhihu.value && !isPlaintext.value)),
)

/**
 * 已发布回看用的 AI 痕迹检测快照：落库在 Content.meta['aigc_trace']，按检测对象
 * 各存一份（layout=排版产物 / body=源文）。发布看的是排版产物那份，所以优先取
 * layout，没有再回落 body。没有快照（发布前没做过检测）就返回 null —— 对应
 * 「如果有当时的检测数据」里的「如果没有」分支。
 *
 * 注意这是**历史快照**，只读展示、不重新检测：朱雀那份若当时没测就是空。
 */
const traceSnapshot = computed(() => {
  const meta = local.value?.meta as Record<string, any> | null | undefined
  const snap = meta?.aigc_trace as Record<string, any> | undefined
  if (!snap) return null
  const slot = (snap.layout || snap.body) as Record<string, any> | undefined
  if (!slot || (!slot.scan && !slot.zhuque)) return null
  return {
    savedAt: (snap.saved_at as number) || undefined,
    platform: (snap.platform as string) || local.value?.platform || '',
    subType: (snap.sub_type as string) || local.value?.sub_type || '',
    source: (slot.source as string) || 'layout',
    scan: (slot.scan as Record<string, any>) || null,
    zhuque: (slot.zhuque as Record<string, any>) || null,
  }
})

/** 检测对象中文名（排版产物 / 源文）。 */
const srcLabel = (s: string) => (s === 'body' ? '源文' : '排版产物')
const scoreColor = (s: number) =>
  s >= 80 ? 'var(--app-success, #18a058)' : s >= 60 ? 'var(--app-warning, #f0a020)' : 'var(--app-error, #d03050)'
const severityType = (s: string) => (s === 'high' ? 'error' : s === 'mid' ? 'warning' : 'default')
const severityLabel = (s: string) => (s === 'high' ? '明显' : s === 'mid' ? '中等' : '轻微')
const pct = (x: number) => `${(x * 100).toFixed(2)}%`
function fmtTime(ts?: number): string {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// 排版任务进度
const progress = ref(0)
const stage = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null
// 同一排版任务只弹一次完成提示（防止极端情况下多个轮询重复弹「完成」）
let announcedTaskId: string | null = null

// 预览宽度三档
type PreviewMode = 'mobile' | 'tablet' | 'desktop'
const previewMode = ref<PreviewMode>('mobile')

const themeOptions = computed(() =>
  themes.value.map((t) => ({
    label: `${t.name} · ${t.scenes}`,
    value: t.id,
  })),
)

const currentThemeInfo = computed(() => themes.value.find((t) => t.id === theme.value))

// 人工痕迹体检抽屉（零 LLM 的体检 + 走模型的「加痕迹」改写）
const traceShow = ref(false)

/**
 * 当前生效的 HTML：优先右栏编辑结果 editedHtml，其次库里 body_html。
 * onCopyRich / onPushDraft 都读它，保证「编辑后复制/推送」拿到的是编辑版。
 */
const html = computed(() => editedHtml.value || local.value?.body_html || '')
const hasHtml = computed(() => !!html.value)
const genReady = ref(true)

// 封面通道能力位 + 默认值（由 theme-options 带回）
const coverReady = ref(false)
const coverStyles = ref<Record<string, string>>({})
const coverDefaultStyle = ref('gradient')
const coverDefaultTag = ref('')
// 排版时是否自动生成封面（用户可关）
const autoCover = ref(true)
// 公众号排版后是否自动在文末补 2-3 个精准 #话题#（用户可关）
const autoTags = ref(true)

/** 预览宽度：手机 375 / 平板 768 / 桌面 100%（不设限）。 */
const previewWidth = computed(() => {
  switch (previewMode.value) {
    case 'mobile':
      return '375px'
    case 'tablet':
      return '768px'
    default:
      return '100%'
  }
})

watch(
  () => props.show,
  async (v) => {
    if (!v) {
      stopPoll()
      // 面板被关掉时若排版仍在进行，通知后端解除「加工中」标记，
      // 否则队列页那一行会被一直禁用（用户实测踩过）。
      if (layouting.value && local.value) {
        layouting.value = false
        try {
          await cancelProcessing(local.value.id)
        } catch {
          // 静默：取消失败也不会让本地卡住，用户还能用「取消」按钮兜底
        }
      }
      return
    }
    if (!props.content) return
    markBodySync()
    local.value = { ...props.content }
    errors.value = []
    warnings.value = []
    progress.value = 0
    stage.value = ''
    dirty.value = false
    editedHtml.value = ''
    editedPlain.value = null
    plainDirty.value = false
    authorTouched.value = false
    theme.value = props.content.theme || 'moyu-green'
    // 右栏初始文档只在打开/排版完成时设一次（编辑期不动，避免光标丢失）
    rebuildPreview()
    nextTick(autosizePlain)
    try {
      const r = await getThemeOptions(props.content.id)
      themes.value = r.themes
      genReady.value = r.gen_ready
      coverReady.value = !!r.cover_ready
      coverStyles.value = r.cover_styles || {}
      coverDefaultStyle.value = r.cover_default_style || 'gradient'
      coverDefaultTag.value = r.cover_default_tag || ''
    } catch (e) {
      message.error(`加载主题失败：${(e as Error).message}`)
    }
    // 文内配图可用的生成通道（脚本 / Cloudflare），决定 picker 里显示哪些生成方式
    try {
      providers.value = await getImageProviders()
      if (!providers.value.script && providers.value.cloudflare) pickerProvider.value = 'cloudflare'
    } catch {
      /* 能力位拿不到就按脚本兜底 */
    }
    // 加载作者默认信息（填进面板，可临时覆盖）
    try {
      const a = await getAuthorInfo()
      authorName.value = a.author_name || ''
      authorIntro.value = a.author_intro || ''
    } catch {
      // 静默：拿不到就用空
    }
    // 加载公众号账号（用于推草稿选号）；失败静默，回退后端默认
    try {
      wechatAccounts.value = await listWeChatAccounts()
      const def = wechatAccounts.value.find((a) => a.is_default)
      accountId.value = def?.id ?? wechatAccounts.value[0]?.id ?? ''
    } catch {
      wechatAccounts.value = []
      accountId.value = ''
    }
  },
)

// 左栏 Markdown 被编辑 → 置脏（NInput 只回写 local.body，不会自己标脏）
// 程序化替换 local（打开另一条内容 / 排版完成刷新 / 痕迹写回）也会触发本 watch，
// 但那不是用户编辑：先 markBodySync() 打标记，让下一次触发被吞掉，
// 否则「打开第二条内容」会被误判成「改了正文」，关面板时误弹「有未保存的修改」。
let swallowBodyWatch = false
function markBodySync() {
  swallowBodyWatch = true
  nextTick(() => {
    swallowBodyWatch = false
  })
}
watch(
  () => local.value?.body,
  (nv, ov) => {
    if (swallowBodyWatch) return
    // 只读态（已发布）没有「保存」这回事，永远不置脏
    if (isReadonly.value) return
    if (nv !== undefined && ov !== undefined && nv !== ov) dirty.value = true
  },
)

// 纯文本改过 → 重算高度（textarea 不会像 pre 那样随内容自己长）
watch(plainDraft, () => nextTick(autosizePlain))

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function onLayout() {
  if (!local.value || layouting.value) return
  // 立即置真：下面有若干 await（存正文、确认弹窗），窗口期内重复点击会绕过下面的守卫、
  // 生成多个排版任务，进而出现「多个完成弹框」。这里先把开关锁死，从源头杜绝重入。
  layouting.value = true
  if (layoutGate.blocked.value) {
    message.warning(layoutGate.reason.value)
    layouting.value = false
    return
  }
  if (local.value.processing === 'layout') {
    message.warning('该内容正在排版中，请稍候')
    layouting.value = false
    return
  }
  // 排版读的是库里的 body —— 左栏改了 Markdown 必须先存回去，否则排的是旧内容
  if (dirty.value && local.value.body !== undefined) {
    try {
      await updateContent(local.value.id, { body: local.value.body })
    } catch (e) {
      message.error(`保存正文失败，已中止排版：${(e as Error).message}`)
      layouting.value = false
      return
    }
  }
  // 右栏手改过（富文本或纯文本）的话，提醒会被排版结果覆盖
  if (editedHtml.value || hasPlainEdit.value) {
    const go = await new Promise<boolean>((resolve) => {
      dialog.warning({
        title: '重新排版会覆盖手动修改',
        content: '你会丢失在右侧预览里的手动编辑，确定继续吗？',
        positiveText: '继续排版',
        negativeText: '取消',
        onPositiveClick: () => resolve(true),
        onNegativeClick: () => resolve(false),
        onClose: () => resolve(false),
      })
    })
    if (!go) {
      layouting.value = false
      return
    }
  }
    // 此时已确认要排版：清掉任何残留轮询，避免旧 interval 与新 interval 并存导致重复弹完成
  stopPoll()
  announcedTaskId = null
  errors.value = []
  warnings.value = []
  progress.value = 2
  stage.value = '提交排版任务…'
  let task: Awaited<ReturnType<typeof layoutContent>> | null = null
  try {
    task = await layoutContent(
      local.value.id,
      theme.value,
      // 按内容自己的平台传：知乎走语义 HTML，头条/百家号走纯文本规整，
      // 其余（含公众号图文）走富文本排版。传错会把头条当公众号排一版 HTML。
      (local.value.platform as 'gzh' | 'xiaohongshu' | 'zhihu' | 'toutiao' | 'baijiahao') || 'gzh',
      // 非公众号类型不生成封面：产物是纯文本/复制带走，封面用不上，
      // 白跑一次无头浏览器截图没意义（开关已隐藏，这里再兜一层）
      showImagePanel.value && autoCover.value,
      authorName.value.trim(),
      authorIntro.value.trim(),
      accountId.value,
      autoTags.value,
    )
    if (!task.ok || !task.task_id) {
      throw new Error(task.message || '提交失败')
    }
    // 乐观置灰：提交即预扣，后端已经记上了
    auth.markUsed('layout')
    // 轮询进度（stopPoll 已确保同一时刻只有一个 interval）
    const tid = task.task_id
    pollTimer = setInterval(() => void poll(tid), 1200)
  } catch (e) {
    layouting.value = false
    progress.value = 0
    stage.value = ''
    message.error(`排版失败：${(e as Error).message}`)
  }
}

async function poll(taskId: string) {
  if (!local.value) return
  try {
    const job = await getLayoutJob(local.value.id, taskId)
    progress.value = job.progress || 0
    stage.value = job.stage || ''
    if (job.status === 'done') {
      stopPoll()
      if (announcedTaskId === taskId) return // 同一任务已完成并提示过，避免重复弹框
      announcedTaskId = taskId
      const res = job.result
      if (res) {
        errors.value = res.errors || []
        warnings.value = res.warnings || []
        local.value = await refreshContent(local.value.id)
        markBodySync()
        // 排版产出了新的 body_html / 新的纯文本 → 丢开编辑态，重建预览
        editedHtml.value = ''
        editedPlain.value = null
        plainDirty.value = false
        dirty.value = false
        rebuildPreview()
        nextTick(autosizePlain)
        if (res.ok) message.success(`排版完成（${res.theme_name}）`)
        else message.warning(`排版有 ${res.errors.length} 处合规问题，可重试`)
      }
      layouting.value = false
      progress.value = 100
      emit('done')
    } else if (job.status === 'error') {
      stopPoll()
      layouting.value = false
      progress.value = 0
      message.error(`排版失败：${job.error || '未知错误'}`)
    }
  } catch (e) {
    // 404 = 任务中断（进程重启后内存任务表清空），或网络抖动
    stopPoll()
    layouting.value = false
    progress.value = 0
    stage.value = ''
    message.error(`排版任务中断：${(e as Error).message}`)
  }
}

function onImagesChanged(images: ContentImage[]) {
  if (local.value) local.value.images = images
  emit('done')
}

/** 复制左栏 Markdown 源文（就是正文本身，不带标题）。 */
async function onCopyMarkdown() {
  const body = local.value?.body || ''
  if (!body.trim()) {
    message.warning('正文还是空的，没东西可复制')
    return
  }
  const ok = await copyText(body)
  message[ok ? 'success' : 'warning'](ok ? 'Markdown 源文已复制' : '复制失败，请手动选择文本复制')
}

/** 复制排版结果（富文本，粘到公众号/知乎编辑器保留格式）。 */
async function onCopyRich() {
  // 纯文本平台（贴图 / 头条 / 百家号）：产物就是这段纯文本本身
  if (isPlaintext.value) {
    // 复制当前看到的内容：手改过就是改过的那版，别把旧产物复制走
    if (!plainDraft.value) {
      message.warning(
        isTietu.value ? '先点「一键排版」规整贴图文案' : '先点「一键排版」规整正文为纯文本',
      )
      return
    }
    const ok = await copyText(plainDraft.value)
    message[ok ? 'success' : 'warning'](
      ok
        ? isTietu.value
          ? '贴图文案已复制，配好图后去公众号后台发「图片」'
          : '正文已复制，粘贴到对应平台后台即可发布'
        : '复制失败，请手动选择文本复制',
    )
    return
  }
  if (!html.value) return
  const r = await copyHtml(html.value, html.value.replace(/<[^>]+>/g, ''))
  if (r === 'rich') {
    message.success(
      isZhihu.value ? '已复制富文本，去知乎编辑器里粘贴（标题与列表保留）' : '已复制富文本，去公众号编辑器粘贴',
    )
    return
  }
  if (r === 'plain') {
    message.success('已复制为纯文本（当前浏览器不支持复制富文本）')
    return
  }
  // 再降级：复制 HTML 源码，用户自己找地方粘
  const ok = await copyText(html.value)
  message[ok ? 'success' : 'warning'](ok ? '已复制 HTML 源码' : '复制失败')
}

/** 推公众号草稿箱：图文传 body_html，贴图传纯文案 + article_type=newspic。 */
async function onPushDraft() {
  if (!local.value) return
  const tietu = isTietu.value
  if (tietu) {
    if (!plainDraft.value.trim()) {
      message.warning('先点「一键排版」生成贴图文案')
      return
    }
    if (tietuImages.value.length === 0) {
      message.warning('贴图至少需要 1 张图片，先在下方「配图」里添加')
      return
    }
  } else if (!html.value) {
    message.warning('先点「一键排版」生成公众号正文')
    return
  }
  if (pushGate.blocked.value) {
    message.warning(pushGate.reason.value)
    return
  }
  pushing.value = true
  try {
    const args: Record<string, unknown> = {
      title: local.value.title,
      content_id: local.value.id,
      author: authorName.value.trim(),
      account_id: accountId.value,
    }
    if (tietu) {
      // 图片消息：微信要求 content 为纯文本，图片由后端从 content.images
      // 逐张上传为永久素材（首图即封面）。传的是当前显示版本，手改未保存也能推。
      args.article_type = 'newspic'
      args.text = plainDraft.value
    } else {
      args.html = html.value
    }
    const res = await confirmTool({
      // 加工面板推送不来自任何对话，不传 conversation_id
      tool_call_id: `q_${local.value.id}`,
      tool_name: 'push_wechat_draft',
      arguments: args,
    })
    const r = res.result as { ok?: boolean; error?: string; message?: string }
    if (r?.ok) {
      message.success(r.message ?? '已推送到草稿箱')
      auth.markUsed('wechat_push')
      emit('done')
    } else {
      // 失败不置灰：后端对这一种有 refund（唯一同步可判成败的受控动作），
      // 本地要是也减一次就成了双重扣账。
      message.error(r?.error ?? '推送失败')
    }
  } catch (e) {
    message.error(`推送失败：${(e as Error).message}`)
  } finally {
    pushing.value = false
  }
}

/**
 * 重建预览文档（只在打开面板、排版完成、手动重置时调用）。
 *
 * 为什么不把 srcdoc 做成响应式 computed：srcdoc 一变 iframe 会整页重载，
 * 正在编辑的光标就会跳回开头。所以这里只在「整篇换掉」的场景手动重建，
 * 平时的编辑通过 input 事件单向回写 editedHtml，不触碰 srcdoc。
 */
function rebuildPreview() {
  const body =
    (local.value?.body_html || '') ||
    '<p style="color:#999;padding:20px">还没有排版结果</p>'
  // 只读态关掉 contenteditable，否则已发布任务还能原地改 HTML
  const editable = isReadonly.value ? 'false' : 'true'
  initialDoc.value = `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{margin:0;padding:16px;background:#fff;font-family:sans-serif;word-break:break-word}
body:focus{outline:none}
</style>
</head><body contenteditable="${editable}">${body}</body></html>`
}

/** iframe 载入后挂 input 监听：防抖把编辑结果回写 editedHtml。 */
function onPreviewLoad() {
  // 只读态不挂监听：contenteditable 已关，挂了也不会触发，但显式跳过更清晰
  if (isReadonly.value) return
  const doc = previewRef.value?.contentDocument
  if (!doc?.body) return
  // 先摘掉旧监听（重建 srcdoc 后 load 会再次触发，避免重复绑定）
  doc.body.oninput = () => {
    dirty.value = true
    if (inputTimer) clearTimeout(inputTimer)
    inputTimer = setTimeout(() => {
      editedHtml.value = doc.body.innerHTML
    }, 300)
  }
  // 预览点击：插入模式 → 在点击位置插图；否则点中 <img> → 弹出替换/删除
  doc.body.removeEventListener('click', onPreviewClick)
  doc.body.addEventListener('click', onPreviewClick)
}

/** 预览区点击分发：插入模式插新图，点或已有图替换。 */
function onPreviewClick(e: MouseEvent) {
  if (isReadonly.value) return
  const doc = previewRef.value?.contentDocument
  if (!doc) return
  if (insertMode.value) {
    // 在点击位置的「块级容器之后」插入（落在该段/该块下方，符合文章插图的落点）
    const ref = blockAtPoint(doc, e.target)
    picker.mode = 'insert'
    picker.insertRef = ref
    picker.tab = 'gen'
    picker.show = true
    insertMode.value = false
    return
  }
  const t = e.target as HTMLElement | null
  const img = t?.closest?.('img') as HTMLImageElement | null
  if (img) {
    picker.mode = 'replace'
    picker.imgEl = img
    picker.tab = 'existing'
    picker.show = true
  }
}

/** 取点击位置所在的块级容器（没有就退回 body），作为插图锚点。 */
function blockAtPoint(doc: Document, target: EventTarget | null): HTMLElement {
  const el = (target as HTMLElement | null)?.closest?.(
    'p,section,div,h1,h2,h3,h4,h5,h6,li,blockquote,figure',
  ) as HTMLElement | null
  return el || doc.body
}

/** 在锚点块之后插入一张图（与 `_inject_images` 产出同款居中自适应 <img>）。 */
function insertImgAfter(ref: HTMLElement, url: string, alt: string) {
  const doc = previewRef.value?.contentDocument
  if (!doc) return
  const p = doc.createElement('p')
  p.setAttribute('style', 'margin:16px 0;text-align:center;')
  const img = doc.createElement('img')
  img.setAttribute('src', url)
  if (alt) img.setAttribute('alt', alt)
  img.setAttribute(
    'style',
    'max-width:100%;height:auto;display:block;margin:0 auto;border-radius:8px;',
  )
  p.appendChild(img)
  ref.parentNode?.insertBefore(p, ref.nextSibling)
}

function setImgSrc(imgEl: HTMLImageElement, url: string, alt: string) {
  if (alt) imgEl.setAttribute('alt', alt)
  imgEl.setAttribute('src', url)
}

/** 替换已有图时，同步把配图库里那条记录的 url 改掉（按旧 src 匹配）。 */
function syncImageUrl(oldSrc: string, url: string, alt: string) {
  if (!local.value) return
  local.value.images = (local.value.images || []).map((i) =>
    i.url === oldSrc ? { ...i, url, alt: alt || i.alt } : i,
  )
}

/** 预览 DOM 被代码改动后：回写 editedHtml 并标脏（程序化改动不触发 input）。 */
function finalizePreviewEdit() {
  const doc = previewRef.value?.contentDocument
  if (doc?.body) editedHtml.value = doc.body.innerHTML
  dirty.value = true
}

/** 把选好的图落到预览（插入 or 替换），并收尾。 */
function applyResult(url: string, alt: string) {
  if (picker.mode === 'insert' && picker.insertRef) {
    insertImgAfter(picker.insertRef, url, alt)
  } else if (picker.mode === 'replace' && picker.imgEl) {
    const oldSrc = picker.imgEl.getAttribute('src') || ''
    setImgSrc(picker.imgEl, url, alt)
    if (oldSrc) syncImageUrl(oldSrc, url, alt)
  }
  finalizePreviewEdit()
  picker.show = false
}

/** 可用作「选择已有」的配图（封面单独管理，不参与文内替换）。 */
const existingImages = computed(() =>
  (local.value?.images || []).filter((i) => i.source !== 'cover'),
)

function toggleInsertMode() {
  if (isReadonly.value) return
  insertMode.value = !insertMode.value
  if (insertMode.value) message.info('已开启插入模式：点击预览中想放图的位置')
}

async function onPickerGenerate() {
  if (!genPrompt.value.trim()) {
    message.warning('先写一句配图描述')
    return
  }
  if (!local.value) return
  generating.value = true
  try {
    const res = await generateImages(local.value.id, {
      prompt: genPrompt.value.trim(),
      count: genCount.value,
      provider: pickerProvider.value,
    })
    local.value.images = res.images
    const img = res.images.find((i) => i.source !== 'cover') || res.images[0]
    applyResult(img.url, img.alt || '')
    genPrompt.value = ''
  } catch (e) {
    message.error(`生成失败：${(e as Error).message}`)
  } finally {
    generating.value = false
  }
}

async function onPickerUpload({ file }: { file: UploadFileInfo }) {
  if (!file.file || !local.value) return
  uploading.value = true
  try {
    const res = await uploadImage(local.value.id, file.file)
    local.value.images = res.images
    const img = res.images[res.images.length - 1]
    applyResult(img.url, img.alt || '')
  } catch (e) {
    message.error(`上传失败：${(e as Error).message}`)
  } finally {
    uploading.value = false
  }
  return false // 阻止 NUpload 默认上传
}

function onPickerExisting(img: ContentImage) {
  applyResult(img.url, img.alt || '')
}

/** 替换模式下：把点中的图从预览里整块移除（仅视觉，不动配图库）。 */
function deleteCurrentImg() {
  const doc = previewRef.value?.contentDocument
  if (doc && picker.imgEl) {
    const wrap = picker.imgEl.closest('p,figure,div') as HTMLElement | null
    if (wrap?.parentNode) wrap.parentNode.removeChild(wrap)
    else picker.imgEl.remove()
  }
  finalizePreviewEdit()
  picker.show = false
}

function onPickerClose(v: boolean) {
  if (!v) {
    // 取消：插入模式已退出；替换模式无悬挂节点，仅清引用
    picker.insertRef = null
    picker.imgEl = null
  }
}

/** 丢弃右栏手动编辑，回到最近一次排版的产物。 */
function resetPreview() {
  if (!editedHtml.value) return
  editedHtml.value = ''
  dirty.value = false
  rebuildPreview()
  nextTick(() => onPreviewLoad())
  message.success('已重置为排版结果')
}

/** 纯文本预览的输入框：跟着内容长高，不然框里自己再滚一层，读起来很难受。 */
function autosizePlain() {
  const el = plainEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

/** 丢弃纯文本的手改，回到排版产物。 */
function resetPlain() {
  if (!hasPlainEdit.value) return
  editedPlain.value = null
  plainDirty.value = false
  nextTick(autosizePlain)
  message.success('已重置为排版结果')
}

/** 保存：把左栏 Markdown + 右栏 HTML 落库。 */
/**
 * 痕迹改写写回源文后：刷新本地内容 + 清掉编辑态。
 * 排版产物已在后端被清空，所以必须把 editedHtml 一起清了——
 * 否则右栏还挂着旧版 HTML，用户以为看到的还是当前内容。
 */
async function onTraceApplied(text: string) {
  if (!local.value) return
  try {
    local.value = await refreshContent(local.value.id)
    markBodySync()
  } catch {
    local.value = { ...local.value, body: text, body_html: '' }
    markBodySync()
  }
  editedHtml.value = ''
  editedPlain.value = null
  plainDirty.value = false
  dirty.value = false
  emit('done')
}

async function onSave() {
  if (!local.value) return
  saving.value = true
  try {
    const patch: { body?: string; body_html?: string } = { body: local.value.body }
    if (html.value) patch.body_html = html.value
    const updated = await updateContent(local.value.id, patch)
    local.value = { ...local.value, ...updated }
    editedHtml.value = ''

    // 纯文本走单独的接口：那边只认 layouts[platform].text，不碰 body_html
    if (plainDirty.value) {
      const saved = await updatePlaintext(
        local.value.id,
        plainKey.value,
        editedPlain.value ?? plaintextText.value,
      )
      local.value = { ...local.value, ...saved }
      editedPlain.value = null
      plainDirty.value = false
      nextTick(autosizePlain)
    }

    dirty.value = false
    message.success('已保存')
    emit('done')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

/** 把当前作者信息存为全局默认（下次排版自动带出）。 */
async function saveAuthorAsDefault() {
  try {
    await updateAuthorInfo({
      author_name: authorName.value.trim(),
      author_intro: authorIntro.value.trim(),
    })
    authorTouched.value = false
    message.success('已存为默认作者信息')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  }
}

/** 关闭面板前的脏检查：有未保存修改就拦一下。 */
function handleShowUpdate(v: boolean) {
  if (v) {
    emit('update:show', true)
    return
  }
  // 只读态（已发布）没有任何能保存的东西：直接关，别拿「未保存」吓人
  if (isReadonly.value) {
    dirty.value = false
    plainDirty.value = false
    emit('update:show', false)
    return
  }
  if (hasChanges.value) {
    dialog.warning({
      title: '有未保存的修改',
      content: '正文或右侧预览有改动尚未保存，确定直接关闭吗？',
      positiveText: '放弃修改',
      negativeText: '继续编辑',
      onPositiveClick: () => {
        dirty.value = false
        plainDirty.value = false
        emit('update:show', false)
      },
    })
    return
  }
  emit('update:show', false)
}

/**
 * Esc 关闭面板：若图片放大预览（.n-image-preview）正开着，
 * 让预览自己关即可，别把整个排版面板也一起关掉。
 * 配合 NModal 的 :close-on-esc="false"，由这里统一决策。
 */
function onPanelEsc() {
  if (document.querySelector('.n-image-preview')) return
  handleShowUpdate(false)
}

onBeforeUnmount(() => {
  stopPoll()
  if (inputTimer) clearTimeout(inputTimer)
})
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :style="
      fullscreen
        ? 'width:100vw;height:100vh;max-width:100vw;border-radius:0'
        : 'width:min(1200px,92vw);max-width:92vw'
    "
    :content-style="fullscreen ? 'height:calc(100vh - 110px);overflow:auto' : 'max-height:76vh;overflow:auto'"
    :title="`排版详情${local ? ' · ' + local.title : ''}`"
    :bordered="false"
    size="small"
    :close-on-esc="false"
    @esc="onPanelEsc"
    @update:show="handleShowUpdate"
  >
    <template #header-extra>
      <NTag v-if="isReadonly" size="small" type="success" :bordered="false" style="margin-right: 8px">
        已发布 · 只读
      </NTag>
      <IconTextBtn
        :name="fullscreen ? 'compress' : 'expand'"
        size="small"
        quaternary
        @click="fullscreen = !fullscreen"
      >
        {{ fullscreen ? '还原' : '全屏' }}
      </IconTextBtn>
    </template>

    <div v-if="local" class="panel">
      <!-- 顶部：主题选择 + 排版操作。只读态（已发布）下这些都不显示，面板只展示内容与各栏标题旁的复制 -->
      <div class="toolbar">
        <template v-if="!isReadonly">
          <!-- 知乎不选主题：主题是公众号内联样式那套，知乎编辑器会把 style 剥掉。
               贴图也不选：图片消息没有富文本，主题概念不适用。 -->
          <NSelect
            v-if="!isZhihu && !isPlaintext"
            v-model:value="theme"
            :options="themeOptions"
            size="small"
            style="width: 260px"
            placeholder="选择排版主题"
            :disabled="layouting"
          />
          <IconTextBtn
            name="magic"
            type="primary"
            :loading="layouting"
            :disabled="layoutGate.blocked.value"
            @click="onLayout"
          >
            {{
              layoutGate.blocked.value
                ? '无法排版'
                : layoutGate.left.value === null
                  ? '一键排版'
                  : `一键排版（剩 ${layoutGate.left.value}）`
            }}
          </IconTextBtn>
          <div v-if="layoutGate.reason.value" class="gate-tip">⚠ {{ layoutGate.reason.value }}</div>
          <NCheckbox
            v-if="showImagePanel && coverReady && !isZhihu && !isPlaintext"
            v-model:checked="autoCover"
            size="small"
            :disabled="layouting"
          >
            排版时自动生成封面
          </NCheckbox>
          <NCheckbox
            v-if="!isZhihu && !isPlaintext"
            v-model:checked="autoTags"
            size="small"
            :disabled="layouting"
          >
            文末自动补话题标签
          </NCheckbox>
          <!-- 痕迹体检：针对**源文**，零 LLM 零成本，所以任何平台都给 -->
          <IconTextBtn name="search" size="small" :disabled="!local?.body" @click="traceShow = true">
            痕迹体检
          </IconTextBtn>
        </template>
        <template v-if="canShowPushDraft">
          <NSelect
            v-if="wechatAccounts.length > 1"
            v-model:value="accountId"
            :options="wechatAccounts.map((a) => ({ label: a.name || a.appid, value: a.id }))"
            size="small"
            style="width: 180px"
            placeholder="选择公众号"
          />
          <NTag v-if="followCardHint" size="small" :bordered="false" type="info">
            {{ followCardHint }}
          </NTag>
          <IconTextBtn
            name="send"
            type="primary"
            ghost
            :disabled="!canPushDraft || layouting || pushGate.blocked.value"
            :loading="pushing"
            @click="onPushDraft"
          >
            {{
              pushGate.blocked.value
                ? '无法推送'
                : pushGate.left.value === null
                  ? isTietu ? '推贴图草稿箱' : '推公众号草稿箱'
                  : `${isTietu ? '推贴图' : '推公众号'}草稿箱（剩 ${pushGate.left.value}）`
            }}
          </IconTextBtn>
          <div v-if="pushGate.reason.value" class="gate-tip">⚠ {{ pushGate.reason.value }}</div>
          <div v-if="isTietu && !canPushDraft" class="gate-tip">
            ⚠ 贴图需先「一键排版」生成文案，并在下方「配图」里至少加 1 张图
          </div>
        </template>
        <!-- 保存对任何平台都要有：知乎用户改完 Markdown 也得能存 -->
        <template v-if="!isReadonly">
          <IconTextBtn
            name="save"
            :type="hasChanges ? 'warning' : 'default'"
            :ghost="hasChanges"
            :loading="saving"
            @click="onSave"
          >
            {{ hasChanges ? '保存修改' : '保存' }}
          </IconTextBtn>
          <NTag v-if="currentThemeInfo && !isZhihu" size="small" :bordered="false">
            {{ currentThemeInfo.desc }}
          </NTag>
        </template>
        <NTag v-else-if="currentThemeInfo" size="small" :bordered="false" type="info">
          {{ currentThemeInfo.name }}
        </NTag>
      </div>

      <!-- 作者信息：排版时替换正文里的 {{作者名}} / {{一句话简介}}（只读态隐藏）。
           贴图不适用：图片消息是短文案，没有作者卡的位置。 -->
      <div v-if="!isReadonly && !isPlaintext" class="author-row">
        <span class="author-label">作者</span>
        <NInput
          v-model:value="authorName"
          size="small"
          style="width: 150px"
          placeholder="作者名"
          :disabled="layouting"
        />
        <NInput
          v-model:value="authorIntro"
          size="small"
          style="width: 280px"
          placeholder="一句话简介（如：专注 AI 观察与干货）"
          :disabled="layouting"
        />
        <IconTextBtn name="star" size="tiny" quaternary @click="saveAuthorAsDefault">存为默认</IconTextBtn>
        <span class="author-hint">
          排版后自动替换正文里的 <code v-text="PH_NAME" /> / <code v-text="PH_INTRO" />；留空则保留占位
        </span>
      </div>

      <!-- 排版进度 -->
      <div v-if="layouting" class="progress-box">
        <NProgress
          type="line"
          :percentage="progress"
          :height="10"
          :border-radius="5"
          processing
        />
        <div class="stage-text">{{ stage || '正在排版…' }}</div>
      </div>

      <!-- 合规校验结果 -->
      <NAlert v-if="errors.length" type="error" :bordered="false" style="margin-top: 10px">
        <template #header>合规校验未通过（{{ errors.length }}）</template>
        <div v-for="(e, i) in errors" :key="i" class="issue">• {{ e }}</div>
      </NAlert>
      <NAlert
        v-else-if="warnings.length"
        type="warning"
        :bordered="false"
        style="margin-top: 10px"
      >
        <template #header>提示（{{ warnings.length }}）</template>
        <div v-for="(w, i) in warnings" :key="i" class="issue">• {{ w }}</div>
      </NAlert>

      <div class="split">
        <!-- 左：原文（可编辑 Markdown；只读态改为渲染展示） -->
        <div class="col">
          <div class="col-title">
            <span>纯内容（Markdown）</span>
            <NTag v-if="isReadonly" size="tiny" type="info" :bordered="false">已发布</NTag>
            <NTag v-else-if="dirty" size="tiny" type="warning" :bordered="false">未保存</NTag>
            <IconTextBtn
              name="copy"
              size="tiny"
              quaternary
              @click="onCopyMarkdown"
            >
              复制
            </IconTextBtn>
          </div>
          <div class="scroll scroll-edit">
            <MarkdownEditor
              v-if="!isReadonly"
              v-model="local.body"
              :disabled="layouting"
              height="420px"
            />
            <MarkdownView v-else :content="local.body || ''" />
          </div>
        </div>
        <!-- 右：纯文本预览（贴图 / 头条 / 百家号；这些平台没有可编辑 HTML） -->
        <div v-if="isPlaintext" class="col">
          <div class="col-title">
            <span>{{ isTietu ? '贴图文案（纯文本）' : '正文（纯文本）' }}</span>
            <NTag v-if="isReadonly" size="tiny" type="info" :bordered="false">已发布</NTag>
            <template v-else>
              <NTag v-if="plaintextText" size="tiny" type="success" :bordered="false">已规整</NTag>
              <NTag v-else size="tiny" :bordered="false">未规整</NTag>
            </template>
            <span v-if="hasPlainEdit" class="edit-flag">已手动编辑</span>
            <IconTextBtn
              name="copy"
              size="tiny"
              quaternary
              @click="onCopyRich"
            >
              复制
            </IconTextBtn>
            <IconTextBtn
              v-if="hasPlainEdit && !isReadonly"
              name="reset"
              size="tiny"
              quaternary
              style="margin-left: 6px"
              @click="resetPlain"
            >
              重置
            </IconTextBtn>
          </div>
          <div class="scroll preview-wrap" :class="{ 'plain-wrap': !isTietu }">
            <!-- 只读态（已发布）：纯展示，别让人以为还能改 -->
            <template v-if="isReadonly">
              <pre v-if="plainDraft" class="plain-text" :class="{ 'is-card': isTietu }">{{
                plainDraft
              }}</pre>
              <div v-else class="plain-empty">
                <template v-if="isTietu">
                  这条内容还没规整过贴图文案。点「一键排版」生成首图标题 + emoji 短点，
                  出来即可直接粘进公众号图片消息。
                </template>
                <template v-else>
                  这条内容还没规整过纯文本正文。排版会把 Markdown 语法符号去掉、收拾分段，
                  出来就是发布端能直接粘贴的稿子。
                </template>
              </div>
            </template>
            <!-- 可编辑：排版产物就是一段字，改起来也该像改字，不该只给看 -->
            <textarea
              v-else
              ref="plainEl"
              v-model="plainDraft"
              class="plain-text plain-editor"
              :class="{ 'is-card': isTietu }"
              spellcheck="false"
              :placeholder="
                isTietu
                  ? '点「一键排版」规整贴图文案，也可以直接在这儿写'
                  : '点「一键排版」规整成纯文本，也可以直接在这儿写'
              "
              @input="autosizePlain"
            ></textarea>
          </div>
        </div>
        <!-- 右：排版预览（所见即所得，可直接编辑；只读态不可编辑） -->
        <div v-else class="col">
          <div class="col-title">
            <span>排版预览</span>
            <NTag v-if="isReadonly" size="tiny" type="info" :bordered="false">已发布</NTag>
            <template v-else>
              <NTag v-if="hasHtml" size="tiny" type="success" :bordered="false">已排版</NTag>
              <NTag v-else size="tiny" :bordered="false">未排版</NTag>
            </template>
            <span v-if="editedHtml" class="edit-flag">已手动编辑</span>
            <IconTextBtn
              name="copy"
              size="tiny"
              quaternary
              @click="onCopyRich"
            >
              复制
            </IconTextBtn>
            <IconTextBtn
              v-if="editedHtml && !isReadonly"
              name="reset"
              size="tiny"
              quaternary
              style="margin-left: 6px"
              @click="resetPreview"
            >
              重置
            </IconTextBtn>
            <!-- 与配图面板同一个门控：非公众号类型不提供插图入口，
                 否则会出现「配图面板没了，却还能往 HTML 里插图」的割裂 -->
            <IconTextBtn
              v-if="!isReadonly && showImagePanel"
              :name="insertMode ? 'check' : 'plus'"
              size="tiny"
              :type="insertMode ? 'primary' : 'default'"
              quaternary
              style="margin-left: 6px"
              :class="{ 'insert-active': insertMode }"
              @click="toggleInsertMode"
            >
              {{ insertMode ? '点预览插入' : '插入配图' }}
            </IconTextBtn>
            <span v-if="insertMode" class="insert-hint">点击预览里要放图的位置</span>
            <NRadioGroup v-model:value="previewMode" size="small" style="margin-left: auto">
              <NRadioButton value="mobile">手机</NRadioButton>
              <NRadioButton value="tablet">平板</NRadioButton>
              <NRadioButton value="desktop">桌面</NRadioButton>
            </NRadioGroup>
          </div>
          <div class="scroll preview-wrap">
            <div class="preview-frame" :style="{ width: previewWidth }">
              <iframe
                ref="previewRef"
                class="preview"
                :srcdoc="initialDoc"
                @load="onPreviewLoad"
              />
            </div>
          </div>
        </div>
      </div>

      <!-- 已发布回看：发布当时的 AI 痕迹检测快照（落库在 meta，只读展示，不重新检测） -->
      <div v-if="isReadonly && traceSnapshot" class="trace-snap">
        <div class="col-title">
          <span>AI 痕迹检测（发布当时）</span>
          <NTag size="tiny" type="info" :bordered="false">{{ srcLabel(traceSnapshot.source) }}</NTag>
          <span class="snap-time" v-if="traceSnapshot.savedAt">{{ fmtTime(traceSnapshot.savedAt) }}</span>
          <span class="snap-hint">历史快照，非实时</span>
        </div>

        <!-- 本地人工痕迹体检：分数 + 档位 + 命中特征 -->
        <div v-if="traceSnapshot.scan" class="snap-card">
          <div class="snap-score-row">
            <div class="snap-score" :style="{ color: scoreColor(traceSnapshot.scan.score) }">
              {{ traceSnapshot.scan.score }}
            </div>
            <div class="snap-score-meta">
              <div class="snap-score-title">人工痕迹指数（越高越像人写的）</div>
              <NTag
                size="small"
                :bordered="false"
                :type="severityType(traceSnapshot.scan.level === 'natural' ? 'low' : 'mid')"
              >
                {{ traceSnapshot.scan.level_label }}
              </NTag>
            </div>
          </div>
          <NAlert type="info" :bordered="false" class="snap-note">{{ traceSnapshot.scan.note }}</NAlert>
          <template v-if="traceSnapshot.scan.hits && traceSnapshot.scan.hits.length">
            <NDivider title-placement="left" class="snap-divider">
              命中的特征（{{ traceSnapshot.scan.hits.length }}）
            </NDivider>
            <ul class="snap-hits">
              <li v-for="h in traceSnapshot.scan.hits" :key="h.key">
                <NTag size="small" :bordered="false" :type="severityType(h.severity)">
                  {{ severityLabel(h.severity) }}
                </NTag>
                <span class="snap-hit-label">{{ h.label }}</span>
                <span class="snap-hit-count">×{{ h.count }}</span>
              </li>
            </ul>
          </template>
        </div>

        <!-- 朱雀 AI 检测：三分类占比（当时若没测则为空，不强行显示） -->
        <div v-if="traceSnapshot.zhuque" class="snap-card">
          <NDivider title-placement="left" class="snap-divider">朱雀 AI 检测（腾讯云）</NDivider>
          <div class="snap-zq">
            <span><i class="dot" style="background: #7bd88f" />人工特征 {{ pct(traceSnapshot.zhuque.human_ratio) }}</span>
            <span><i class="dot" style="background: #f6c15a" />疑似AI {{ pct(traceSnapshot.zhuque.suspect_ratio) }}</span>
            <span><i class="dot" style="background: #f27b8e" />AI特征 {{ pct(traceSnapshot.zhuque.ai_ratio) }}</span>
          </div>
          <div class="snap-zq-meta">
            整体置信度 {{ (traceSnapshot.zhuque.softmax_confidence || 0).toFixed(4) }}
          </div>
        </div>
      </div>

      <template v-if="showImagePanel">
        <NDivider style="margin: 14px 0 8px">配图</NDivider>
        <ImageManager
          :content-id="local.id"
          :images="local.images || []"
          :gen-ready="genReady"
          :cover-ready="coverReady"
          :cover-styles="coverStyles"
          :cover-default-style="coverDefaultStyle"
          :cover-default-tag="coverDefaultTag"
          :body="local.body || ''"
          :theme="theme"
          :readonly="isReadonly"
          @changed="onImagesChanged"
        />
      </template>
    </div>
    <NEmpty v-else description="未选择内容" />
  </NModal>

  <!-- 配图来源选择器：预览「插入 / 替换」时弹出的来源（AI 生成 / 上传 / 选择已有） -->
  <NModal
    v-model:show="picker.show"
    preset="card"
    :title="picker.mode === 'replace' ? '替换配图' : '插入配图'"
    size="small"
    :style="'width:min(560px,92vw)'"
    @update:show="onPickerClose"
  >
    <div class="picker">
      <NTabs type="segment" v-model:value="picker.tab">
        <NTabPane name="gen" tab="AI 生成">
          <div class="picker-row">
            <NInput
              v-model:value="genPrompt"
              class="prompt-input"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 5 }"
              placeholder="配图描述，如：一杯咖啡旁的笔记本电脑，温暖光线，桌面平铺俯拍"
              :disabled="generating"
            />
          </div>
          <div class="picker-row gen-opts">
            <NRadioGroup v-model:value="pickerProvider" size="small">
              <NRadioButton v-if="providers.script" value="script">脚本生成</NRadioButton>
              <NRadioButton v-if="providers.cloudflare" value="cloudflare">Cloudflare AI</NRadioButton>
            </NRadioGroup>
            <span class="count-label">张数</span>
            <NInputNumber v-model:value="genCount" size="small" :min="1" :max="4" style="width: 92px" />
            <IconTextBtn
              name="image"
              type="primary"
              :loading="generating"
              :disabled="!genReady"
              @click="onPickerGenerate"
            >
              生成
            </IconTextBtn>
          </div>
          <div v-if="!genReady" class="hint warn">
            未配置生图脚本（<code>IMAGE_SCRIPT</code>），可改用「上传」或「选择已有」。
          </div>
        </NTabPane>
        <NTabPane name="upload" tab="上传图片">
          <NUpload
            :show-file-list="false"
            :custom-request="onPickerUpload as never"
            accept="image/*"
            :disabled="uploading"
          >
            <IconTextBtn name="upload" :loading="uploading">选择本地图片</IconTextBtn>
          </NUpload>
          <div class="hint-inline">上传后自动插入 / 替换到预览中的目标位置</div>
        </NTabPane>
        <NTabPane name="existing" tab="选择已有">
          <div v-if="existingImages.length" class="existing-grid">
            <div
              v-for="img in existingImages"
              :key="img.id"
              class="existing-cell"
              :title="img.alt || '配图'"
              @click="onPickerExisting(img)"
            >
              <img :src="img.url" :alt="img.alt" />
            </div>
          </div>
          <NEmpty v-else size="small" description="还没有可用配图，先生成或上传" />
        </NTabPane>
      </NTabs>

      <div v-if="picker.mode === 'replace'" class="picker-foot">
        <IconTextBtn name="trash" type="error" size="small" quaternary @click="deleteCurrentImg">
          删除这张图
        </IconTextBtn>
        <span class="hint-inline">仅从排版预览移除，不影响配图库</span>
      </div>
    </div>
  </NModal>

  <!-- 人工痕迹体检：独立抽屉，挂在面板外，关掉面板也不受影响 -->
  <HumanTracePanel
    v-model:show="traceShow"
    :content-id="local?.id || ''"
    :platform="local?.platform || 'gzh'"
    :sub-type="local?.sub_type || ''"
    :text="local?.body || ''"
    @applied="onTraceApplied"
  />
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 权限/配额挡住时的常驻说明 */
.gate-tip {
  flex-basis: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: #d03050;
}
.author-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.author-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-3);
}
.author-hint {
  font-size: 11.5px;
  color: var(--app-text-3);
}
.author-hint code {
  background: rgba(128, 128, 128, 0.14);
  border-radius: 3px;
  padding: 0 3px;
}
.edit-flag {
  font-size: 11px;
  color: var(--app-amber-text, #b8860b);
  margin-left: 6px;
}
.insert-active {
  outline: 1px dashed var(--app-primary, #18a058);
}
.insert-hint {
  font-size: 11.5px;
  color: var(--app-primary, #18a058);
  margin-left: 4px;
}
/* ---------- 配图来源选择器 ---------- */
.picker {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.picker-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 描述文本域独占整行（块级容器里的 NInput 不会自动拉伸） */
.prompt-input {
  width: 100%;
}
.count-label {
  font-size: 12px;
  color: var(--app-text-3);
  flex-shrink: 0;
}
.existing-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 10px;
  margin-top: 4px;
}
.existing-cell {
  aspect-ratio: 4 / 3;
  border: 1px solid var(--app-border, #e0e0e6);
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  background: rgba(128, 128, 128, 0.08);
  transition: border-color 0.15s ease, transform 0.1s ease;
}
.existing-cell:hover {
  border-color: var(--app-primary, #18a058);
  transform: translateY(-1px);
}
.existing-cell img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.picker-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
  padding-top: 8px;
  border-top: 1px dashed var(--app-border, #e0e0e6);
}
/* 左栏现在放编辑器：去掉内边距，让编辑器撑满 */
.scroll-edit {
  padding: 8px 10px;
}
.progress-box {
  margin-top: 12px;
}
.stage-text {
  margin-top: 6px;
  font-size: 12.5px;
  color: var(--app-text-3);
}
.issue {
  font-size: 12.5px;
  line-height: 1.6;
}
.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 12px;
}
.col {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.col-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-3);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.scroll {
  height: 460px;
  overflow: auto;
  border: 1px solid var(--app-border, #e0e0e6);
  border-radius: 8px;
  padding: 12px;
  /* 左栏是 HTML 源码展示区，跟随主题；右栏 iframe 里的文章预览恒为白底 */
  background: var(--app-surface);
}
.preview-wrap {
  display: flex;
  justify-content: center;
  background: var(--app-bg);
  padding: 12px 8px;
}
/* 头条/百家号的预览区去掉外层框：要的是纯文本本身，不是「一个装文本的框」。
   `.scroll` 默认带 1px 边框 + 圆角 + 表面色，对编辑器合适，对一段待复制的
   文案就是多余的边界 —— 复制出去的字本来就没有框。 */
.preview-wrap.plain-wrap {
  background: transparent;
  border: none;
  border-radius: 0;
  padding: 10px 0;
}
/* 纯文本预览：预览的就是「粘贴出去的那段字」本身，所以不给卡片、不给底色。
   文字颜色跟主题走，否则浅色主题下白底黑字、深色主题下就成了黑底黑字。 */
.plain-text {
  width: 100%;
  margin: 0;
  padding: 0;
  font-size: 15px;
  line-height: 1.9;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  color: var(--app-text);
}
/* 贴图文案是「图片消息」成品，保留白底卡片来模拟手机里的观感 */
.plain-text.is-card {
  max-width: 420px;
  padding: 18px 20px;
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.08);
  color: #333;
}
/* 可编辑态：外观和上面的纯文本完全一致，只是把「字」换成能敲进去的框。
   border/outline 全部去掉，否则又变回一个输入框；高度由 autosizePlain 接管。 */
.plain-editor {
  display: block;
  box-sizing: border-box;
  min-height: 160px;
  border: none;
  outline: none;
  resize: none;
  overflow: hidden;
  background: transparent;
}
.plain-editor::placeholder {
  color: var(--app-text-3, #999);
}
.plain-editor.is-card {
  background: #fff;
}
.plain-empty {
  width: 100%;
  align-self: flex-start;
  color: var(--app-text-3, #999);
  font-size: 13px;
  line-height: 2;
}
.preview-frame {
  max-width: 100%;
  height: 100%;
  transition: width 0.2s ease;
  background: #fff;
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.08);
  border-radius: 6px;
  overflow: hidden;
}
.preview {
  width: 100%;
  height: 100%;
  border: none;
}
/* ---------- 已发布回看：AI 痕迹检测快照 ---------- */
.trace-snap {
  margin-top: 6px;
}
.snap-time {
  font-size: 11.5px;
  color: var(--app-text-3);
  font-variant-numeric: tabular-nums;
}
.snap-hint {
  font-size: 11.5px;
  color: var(--app-text-3);
  margin-left: auto;
}
.snap-card {
  border: 1px solid var(--app-border, #e0e0e6);
  border-radius: 8px;
  padding: 12px 14px;
  margin-top: 10px;
  background: var(--app-surface);
}
.snap-score-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.snap-score {
  flex: none;
  white-space: nowrap;
  font-size: 44px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.snap-score-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.snap-score-title {
  font-weight: 600;
  font-size: 13px;
}
.snap-note {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
}
.snap-divider {
  margin: 14px 0 8px;
}
.snap-hits {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.snap-hits li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.snap-hit-label {
  font-weight: 600;
}
.snap-hit-count {
  font-size: 12px;
  opacity: 0.6;
  font-variant-numeric: tabular-nums;
}
.snap-zq {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 13px;
}
.snap-zq .dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  margin-right: 4px;
}
.snap-zq-meta {
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.55;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 768px) {
  .split {
    grid-template-columns: 1fr;
  }
  .scroll {
    height: 320px;
  }
}
</style>
