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
import { updateContent } from '@/api/queue'
import { confirmTool } from '@/api/tools'
import { getAuthorInfo, listWeChatAccounts, updateAuthorInfo } from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'
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
    local.value = { ...props.content }
    errors.value = []
    warnings.value = []
    progress.value = 0
    stage.value = ''
    dirty.value = false
    editedHtml.value = ''
    authorTouched.value = false
    theme.value = props.content.theme || 'moyu-green'
    // 右栏初始文档只在打开/排版完成时设一次（编辑期不动，避免光标丢失）
    rebuildPreview()
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
watch(
  () => local.value?.body,
  (nv, ov) => {
    if (nv !== undefined && ov !== undefined && nv !== ov) dirty.value = true
  },
)

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
  // 右栏手改过 HTML 的话，提醒会被排版结果覆盖
  if (editedHtml.value) {
    const go = await new Promise<boolean>((resolve) => {
      dialog.warning({
        title: '重新排版会覆盖手动修改',
        content: '你会丢失在右侧「排版预览」里的手动编辑，确定继续吗？',
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
      'gzh',
      autoCover.value,
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
        // 排版产出了新的 body_html → 丢开编辑态，重建预览
        editedHtml.value = ''
        dirty.value = false
        rebuildPreview()
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

/** 复制排版结果（富文本，粘到公众号编辑器保留样式）。 */
async function onCopyRich() {
  if (!html.value) return
  try {
    const blob = new Blob([html.value], { type: 'text/html' })
    const text = new Blob([html.value.replace(/<[^>]+>/g, '')], { type: 'text/plain' })
    await navigator.clipboard.write([
      new ClipboardItem({ 'text/html': blob, 'text/plain': text }),
    ])
    message.success('已复制富文本，去公众号编辑器粘贴')
  } catch {
    // 降级：复制源码
    const ok = await navigator.clipboard.writeText(html.value).then(() => true).catch(() => false)
    message[ok ? 'success' : 'warning'](ok ? '已复制 HTML 源码' : '复制失败')
  }
}

/** 推公众号草稿箱（用排版后的 body_html）。 */
async function onPushDraft() {
  if (!local.value) return
  if (!html.value) {
    message.warning('先点「一键排版」生成公众号正文')
    return
  }
  if (pushGate.blocked.value) {
    message.warning(pushGate.reason.value)
    return
  }
  pushing.value = true
  try {
    const res = await confirmTool({
      // 加工面板推送不来自任何对话，不传 conversation_id
      tool_call_id: `q_${local.value.id}`,
      tool_name: 'push_wechat_draft',
      arguments: {
        title: local.value.title,
        html: html.value,
        content_id: local.value.id,
        author: authorName.value.trim(),
        account_id: accountId.value,
      },
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

/** 保存：把左栏 Markdown + 右栏 HTML 落库。 */
async function onSave() {
  if (!local.value) return
  saving.value = true
  try {
    const patch: { body?: string; body_html?: string } = { body: local.value.body }
    if (html.value) patch.body_html = html.value
    const updated = await updateContent(local.value.id, patch)
    local.value = { ...local.value, ...updated }
    editedHtml.value = ''
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
  if (dirty.value) {
    dialog.warning({
      title: '有未保存的修改',
      content: '左侧正文或右侧排版有改动尚未保存，确定直接关闭吗？',
      positiveText: '放弃修改',
      negativeText: '继续编辑',
      onPositiveClick: () => {
        dirty.value = false
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
      <!-- 顶部：主题选择 + 排版（只读态改为纯操作栏：只留复制/预览宽度） -->
      <div class="toolbar">
        <template v-if="!isReadonly">
          <NSelect
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
            v-if="coverReady"
            v-model:checked="autoCover"
            size="small"
            :disabled="layouting"
          >
            排版时自动生成封面
          </NCheckbox>
          <NCheckbox
            v-model:checked="autoTags"
            size="small"
            :disabled="layouting"
          >
            文末自动补话题标签
          </NCheckbox>
        </template>
        <IconTextBtn name="copy" size="small" :disabled="!hasHtml" @click="onCopyRich">复制富文本</IconTextBtn>
        <template v-if="!isReadonly">
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
            :disabled="!hasHtml || layouting || pushGate.blocked.value"
            :loading="pushing"
            @click="onPushDraft"
          >
            {{
              pushGate.blocked.value
                ? '无法推送'
                : pushGate.left.value === null
                  ? '推公众号草稿箱'
                  : `推公众号草稿箱（剩 ${pushGate.left.value}）`
            }}
          </IconTextBtn>
          <div v-if="pushGate.reason.value" class="gate-tip">⚠ {{ pushGate.reason.value }}</div>
          <IconTextBtn
            name="save"
            :type="dirty ? 'warning' : 'default'"
            :ghost="dirty"
            :loading="saving"
            @click="onSave"
          >
            {{ dirty ? '保存修改' : '保存' }}
          </IconTextBtn>
          <NTag v-if="currentThemeInfo" size="small" :bordered="false">
            {{ currentThemeInfo.desc }}
          </NTag>
        </template>
        <NTag v-else-if="currentThemeInfo" size="small" :bordered="false" type="info">
          {{ currentThemeInfo.name }}
        </NTag>
      </div>

      <!-- 作者信息：排版时替换正文里的 {{作者名}} / {{一句话简介}}（只读态隐藏） -->
      <div v-if="!isReadonly" class="author-row">
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
            <NTag v-if="dirty" size="tiny" type="warning" :bordered="false">未保存</NTag>
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
        <!-- 右：排版预览（所见即所得，可直接编辑；只读态不可编辑） -->
        <div class="col">
          <div class="col-title">
            <span>排版预览</span>
            <NTag v-if="hasHtml" size="tiny" type="success" :bordered="false">已排版</NTag>
            <NTag v-else size="tiny" :bordered="false">未排版</NTag>
            <span v-if="editedHtml" class="edit-flag">已手动编辑</span>
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
            <IconTextBtn
              v-if="!isReadonly"
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
@media (max-width: 768px) {
  .split {
    grid-template-columns: 1fr;
  }
  .scroll {
    height: 320px;
  }
}
</style>
