<script setup lang="ts">
/**
 * 视频生产页。
 *
 * 两条入口共用同一条渲染管线：
 *   1. 文案转视频：手写/粘贴口播稿 → 提交 → 轮询 → 在线预览/下载
 *   2. PPT 转视频：上传 pptx → 解析出讲稿 → 填回输入框让用户改 → 再提交
 *
 * 后端是真渲染（edge-tts 配音 + 本地画面 + ffmpeg 合成），任务落库持久化，
 * 刷新页面历史不丢。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NProgress,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  NUpload,
  useMessage,
  type UploadFileInfo,
} from 'naive-ui'

import {
  deleteRenderJob,
  getRenderJob,
  getRenderProgress,
  listRenderJobs,
  parsePpt,
  retryRenderJob,
  submitRender,
  updateRenderJob,
} from '@/api/tools'
import type { VisualScene } from '@/api/tools'
import { getApiBase } from '@/api/config'
import type { JobStatus, RenderJob } from '@/types'
import VoiceClonePanel from '@/components/VoiceClonePanel.vue'
import ActionBtn from '@/components/ActionBtn.vue'
import IconSvg from '@/components/IconSvg.vue'
import { listCloneVoices } from '@/api/tools'
import type { CloneVoice } from '@/api/tools'
import { useAuthStore } from '@/stores/auth'
import { usePermGate } from '@/composables/usePermGate'

const message = useMessage()
const auth = useAuthStore()

/** 提交渲染的门控：权限 video.render + 配额 video（重试也走同一条，后端同样扣） */
const renderGate = usePermGate({ perm: 'video.render', quota: 'video' })
/** PPT 解析的门控：只受权限管控，不计次（它只是个本地转换，不烧 GPU） */
const pptGate = usePermGate({ perm: 'video.ppt_parse' })

/* ---------------------------------------------------------------- 表单 */
const plainScript = ref('') // 纯文案模式的手稿
const ratio = ref('9:16')
const voice = ref('zh-CN-XiaoxiaoNeural')
// PPT 模式：每页标题+要点+该页口播稿，逐页编辑；null 表示纯文案模式
const pptVisualScenes = ref<VisualScene[] | null>(null)
// PPT 文件名（自动命名用）
const pptTitle = ref('')
// 用户点过「停止刷新」后为 true，阻止 refreshList 把轮询自动拉起来
const pollMuted = ref(false)
// 正在轮询的任务 id（防止同一任务起多个轮询）与已弹过结果提示的任务 id
const pollingJobId = ref<string | null>(null)
const toastedJobs = new Set<string>()
const subtitle = ref(true)
const submitting = ref(false)

// 只列 edge-tts 官方在册音色（GET /video/voices 可拿全量清单）。
// 网上流传但已下线的名字（如 zh-CN-XiaochenNeural）会让服务端返回空音频。
const VOICES = [
  { label: '晓晓 · 女声 · 通用（推荐）', value: 'zh-CN-XiaoxiaoNeural' },
  { label: '云希 · 男声 · 通用', value: 'zh-CN-YunxiNeural' },
  { label: '晓伊 · 女声 · 温柔', value: 'zh-CN-XiaoyiNeural' },
  { label: '云健 · 男声 · 沉稳', value: 'zh-CN-YunjianNeural' },
  { label: '云扬 · 男声 · 新闻播报', value: 'zh-CN-YunyangNeural' },
  { label: '云夏 · 男声 · 活泼少年', value: 'zh-CN-YunxiaNeural' },
  { label: '小北 · 女声 · 东北话', value: 'zh-CN-liaoning-XiaobeiNeural' },
  { label: '小妮 · 女声 · 陕西话', value: 'zh-CN-shaanxi-XiaoniNeural' },
]

/** 已复刻的音色（录音克隆出来的），和官方音色一起出现在选择器里 */
const cloneVoices = ref<CloneVoice[]>([])

/** 官方音色 + 复刻音色，复刻的排在前面 —— 用户自己录的显然更想用 */
const voiceOptions = computed(() => {
  const cloned = cloneVoices.value.map((c) => ({
    label: `我的音色 · ${c.name}`,
    value: c.value,
  }))
  return [...cloned, ...VOICES]
})

async function loadCloneVoices() {
  try {
    const r = await listCloneVoices()
    cloneVoices.value = r.items
    // 正在用的复刻音色被删了（别处删的），退回默认官方音色，避免渲染时才报错
    if (voice.value.startsWith('clone_') && !r.items.some((x) => x.value === voice.value)) {
      voice.value = 'zh-CN-XiaoxiaoNeural'
    }
  } catch {
    // 复刻接口挂了不该影响主流程（纯文案渲染完全用不到它）
    cloneVoices.value = []
  }
}

const RATIOS = [
  { label: '9:16 竖屏', value: '9:16', desc: '抖音 / 小红书视频 / 视频号' },
  { label: '16:9 横屏', value: '16:9', desc: 'B 站 / 公众号内嵌 / PPT 转视频' },
  { label: '1:1 方形', value: '1:1', desc: '朋友圈 / 部分信息流' },
]

/** 比例示意图小盒子的尺寸（固定 26px 外接框，按比例缩）。 */
function ratioShape(v: string) {
  const [w, h] = v.split(':').map(Number)
  const box = 26
  const pw = w >= h ? box : Math.round((box * w) / h)
  const ph = w >= h ? Math.round((box * h) / w) : box
  return { width: `${pw}px`, height: `${ph}px` }
}

/** 音色展示名：官方音色取第一段，复刻音色显示「我的音色 · 名字」。 */
function voiceLabel(v: string): string {
  if (v.startsWith('clone_')) {
    const c = cloneVoices.value.find((x) => x.value === v)
    return c ? `我的音色·${c.name}` : '我的音色'
  }
  return VOICES.find((x) => x.value === v)?.label.split(' · ')[0] ?? v
}

/** 实际提交/渲染用的口播稿：PPT 模式按页拼「第N页。<口播>」，纯文案即原文。 */
const script = computed(() =>
  pptVisualScenes.value?.length
    ? pptVisualScenes.value
        .map((p, i) => `第${i + 1}页。${(p.narration || '').trim()}`)
        .filter((x) => x.length > 5)
        .join('\n')
    : plainScript.value,
)

/** 任务自动命名：带画面比例 + 音色，前缀足够区分任务。 */
function buildTitle(): string {
  const head = `${ratio.value}·${voiceLabel(voice.value)}`
  if (pptTitle.value) return `${head} ${pptTitle.value}`
  const body = script.value.replace(/\s+/g, '').slice(0, 12)
  return `${head} ${body || '未命名'}`
}

/* 每页卡片缩略图的 URL 缓存：标题/要点不变就不重新请求 */
const cardUrlCache = new Map<string, string>()
function cardPreviewUrl(i: number, p: VisualScene): string {
  const total = pptVisualScenes.value?.length ?? 1
  const key = `${i}/${total}|${p.title}|${p.body.join('¶')}`
  let url = cardUrlCache.get(key)
  if (!url) {
    const q = new URLSearchParams({ idx: String(i), total: String(total), title: p.title })
    if (p.body.length) q.set('body', p.body.join('\n'))
    if (p.slide_image) q.set('slide_image', p.slide_image)
    const base = getApiBase()
    url = base.startsWith('http') ? `${base}/video/preview-card?${q}` : `/api/video/preview-card?${q}`
    cardUrlCache.set(key, url)
  }
  return url
}

/** 真实幻灯片原图的访问 URL（后端 /slide-images/ 提供）。 */
function slideImgUrl(rel: string): string {
  const base = getApiBase()
  const path = `/video/slide-images/${rel.replace(/^slides\//, '')}`
  return base.startsWith('http') ? `${base}${path}` : `/api${path}`
}

/* 大图预览：点缩略图弹层看原图 */
const previewSrc = ref('')
const previewCaption = ref('')
const previewOpen = ref(false)
function openPreview(src: string, caption: string) {
  previewSrc.value = src
  previewCaption.value = caption
  previewOpen.value = true
}

/* ---------------------------------------------------------------- 任务 */
const job = ref<RenderJob | null>(null)      // 当前正在盯的任务
const history = ref<RenderJob[]>([])
// 历史任务分页：任务会越攒越多，只拉当页
const PAGE_SIZE = 10
const page = ref(1)
const pageCount = ref(1)
const totalJobs = ref(0)
// 过滤条件：状态 + 标题关键词（改动后自动回第 1 页重查）
const filterStatus = ref<'all' | JobStatus>('all')
const filterKeyword = ref('')
let searchTimer: number | null = null
function onFilterChange() {
  page.value = 1
  void refreshList()
}
function onSearchInput() {
  // 输入防抖，别每敲一个字就打一次接口
  if (searchTimer !== null) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(onFilterChange, 300)
}
const polling = ref(false)
let timer: number | null = null

const STATUS_FILTERS: { label: string; value: string }[] = [
  { label: '全部', value: 'all' },
  { label: '渲染中', value: 'running' },
  { label: '已完成', value: 'done' },
  { label: '失败', value: 'failed' },
]

const statusMeta: Record<JobStatus, { label: string; type: 'default' | 'info' | 'success' | 'error' }> = {
  queued: { label: '排队中', type: 'default' },
  running: { label: '渲染中', type: 'info' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'error' },
}

const wordCount = computed(() => script.value.replace(/\s/g, '').length)
const estimateSeconds = computed(() => Math.max(1, Math.round(wordCount.value / 4.5)))

/** 后端返回的 output_url 是 /api/… 相对路径。
 *  静态通道下前端和后端不同源，得拼上用户配置的后端地址。 */
function mediaUrl(rel: string): string {
  if (!rel) return ''
  const base = getApiBase()
  return base.startsWith('http') ? base + rel.replace(/^\/api/, '') : rel
}

function fmtSize(bytes: number): string {
  if (!bytes) return '—'
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.round(bytes / 1024)} KB`
}
function fmtDur(sec: number): string {
  if (!sec) return '—'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return m ? `${m}分${s}秒` : `${s}秒`
}
function fmtTime(ts: number | null): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function onSubmit() {
  if (!script.value.trim()) {
    message.warning('先写上口播稿，或者去对话创作页让 AI 生成一版')
    return
  }
  if (renderGate.blocked.value) {
    message.warning(renderGate.reason.value)
    return
  }
  submitting.value = true
  try {
    const res = await submitRender({
      script: script.value,
      ratio: ratio.value,
      voice: voice.value,
      subtitle: subtitle.value,
      title: buildTitle(),
      visual_scenes: pptVisualScenes.value ?? undefined,
    })
    if (res.ok) {
      message.success('任务已提交，正在渲染')
      // 乐观置灰：提交即预扣（后端 require_use 里已经记上了）
      auth.markUsed('video')
      await refreshList()
      await poll(res.job_id)
    } else {
      message.error('提交失败')
    }
  } catch (e) {
    message.error(`提交失败：${(e as Error).message}`)
  } finally {
    submitting.value = false
  }
}

/**
 * 轮询任务状态，直到 done / failed 或超过 3 分钟。
 *
 * 三个优化（经 Cloudflare Tunnel 单次请求要 1.7~2.9 秒，轮询代价很高）：
 * 1. 走轻量接口 /jobs/{id}/progress —— 响应从 22KB 压到约 200 字节；
 * 2. 自适应间隔：进度有变化就快问，没变化就慢慢退避（4s → 8s → 15s 封顶），
 *    避免复刻音色那种「一段要 40 秒」的长任务把带宽全耗在空轮询上；
 * 3. 只在结束时取一次完整详情，回填 script / visual_scenes。
 */
async function poll(jobId: string) {
  // 同一任务已经在盯了就别起第二个轮询 —— 否则会重复弹「渲染完成」
  if (polling.value && pollingJobId.value === jobId) return
  stopPoll()
  pollMuted.value = false
  polling.value = true
  pollingJobId.value = jobId
  const deadline = Date.now() + 3 * 60 * 60 * 1000
  let lastProgress = -1
  let idleTicks = 0

  const tick = async () => {
    try {
      const p = await getRenderProgress(jobId)
      // 轻量结果只覆盖进度字段，保留本地的 script / visual_scenes，
      // 否则轮询会把编辑区里的大字段冲掉
      if (job.value && job.value.id === jobId) {
        job.value = { ...job.value, ...p }
      }
      upsertHistory(p)

      if (p.status === 'done' || p.status === 'failed') {
        stopPoll()
        // 结束时补一次完整详情，把 script / visual_scenes 拉回来
        try {
          const full = await getRenderJob(jobId)
          job.value = full
          upsertHistory(full)
        } catch {
          /* 回填失败不影响结果提示 */
        }
        // 同一个任务只弹一次结果提示（多入口可能重复触发轮询的收尾）
        if (!toastedJobs.has(jobId)) {
          toastedJobs.add(jobId)
          message[p.status === 'done' ? 'success' : 'error'](
            p.status === 'done'
              ? `渲染完成，时长 ${fmtDur(p.duration_sec)}`
              : `渲染失败：${p.error}`,
          )
        }
        await refreshList()
        return
      }
      if (Date.now() > deadline) {
        stopPoll()
        message.warning('轮询超时，任务可能仍在后台运行')
        return
      }

      // 自适应间隔：进度变了说明在推进，保持快问；没变就退避
      if (p.progress !== lastProgress) {
        lastProgress = p.progress
        idleTicks = 0
      } else {
        idleTicks += 1
      }
      scheduleNext(idleTicks)
    } catch (e) {
      stopPoll()
      message.error(`查询任务失败：${(e as Error).message}`)
    }
  }

  // 4s / 8s / 15s 三档，空转越久问得越稀
  const scheduleNext = (idle: number) => {
    const delay = idle === 0 ? 4000 : idle <= 2 ? 8000 : 15000
    if (timer !== null) window.clearTimeout(timer)
    timer = window.setTimeout(tick, delay)
  }

  await tick()
}

function stopPoll() {
  if (timer !== null) {
    window.clearTimeout(timer)
    window.clearInterval(timer)
    timer = null
  }
  polling.value = false
  pollingJobId.value = null
  pollMuted.value = true
}

/**
 * 只更新当前页里已有的那条，不做插入 —— 否则会打乱分页顺序。
 *
 * 接受 Partial：轮询给的精简进度只有部分字段，按字段合并而不是整条替换，
 * 免得把列表项里的 script / visual_scenes 冲掉。
 */
function upsertHistory(j: Partial<RenderJob> & { id: string }) {
  const idx = history.value.findIndex((x) => x.id === j.id)
  if (idx >= 0) history.value[idx] = { ...history.value[idx], ...j }
}

async function refreshList() {
  try {
    const res = await listRenderJobs(
      page.value,
      PAGE_SIZE,
      filterStatus.value,
      filterKeyword.value,
    )
    history.value = res.items
    totalJobs.value = res.total
    pageCount.value = res.pages
    // 后端页码可能被钳过（比如删任务后当前页超界），以它为准
    if (res.page !== page.value) {
      page.value = res.page
      return refreshList()
    }
    // 页面刚进来时，把最近一个任务设为当前，恢复上下文
    if (!job.value && res.items.length) {
      job.value = res.items[0]
    }
    // 有还在跑的任务就接着盯它（页面刷新后恢复轮询）。
    // 但用户点过「停止刷新」就不再自动恢复， mute 由显式 poll()/selectJob 解除。
    const active = res.items.find((j) => j.status === 'running' || j.status === 'queued')
    if (active && !polling.value && !pollMuted.value) {
      job.value = active
      void poll(active.id)
    }
  } catch (e) {
    message.error(`加载任务列表失败：${(e as Error).message}`)
  }
}

function goPage(p: number) {
  const target = Math.min(Math.max(1, p), pageCount.value)
  if (target === page.value) return
  page.value = target
  void refreshList()
}

/** 把任务的口播稿/每页结构填回左侧编辑区（点历史任务时调用）。 */
function echoJob(j: RenderJob) {
  if (j.visual_scenes?.length) {
    pptVisualScenes.value = j.visual_scenes.map((v) => ({ ...v }))
    pptTitle.value = j.source === 'ppt' ? j.title.replace(/^\S+?·\S+?\s/, '') : ''
  } else {
    pptVisualScenes.value = null
    pptTitle.value = ''
    plainScript.value = j.script || ''
  }
}

function selectJob(j: RenderJob) {
  stopPoll()
  job.value = j
  echoJob(j)
  if (j.status === 'running' || j.status === 'queued') {
    void poll(j.id)
  }
}

async function onRetry(j: RenderJob) {
  try {
    // 编辑区里改过的内容（稿/每页结构/名称）先存回任务，再沿新参数重跑
    if (job.value?.id === j.id) {
      const updated = await updateRenderJob(j.id, {
        title: renameDraft.value || j.title,
        script: script.value,
        visual_scenes: pptVisualScenes.value ?? undefined,
      })
      job.value = updated
      upsertHistory(updated)
    }
    const nj = await retryRenderJob(j.id)
    message.success('已重新提交')
    // 重试也要记一次：后端 job_retry 上挂着同一个配额依赖，漏了这里
    // 前端显示的数字就会比后端少，用户以为还能跑结果被 429。
    auth.markUsed('video')
    // 重试沿用的同一个任务 id，清掉提示标记让新结果能正常弹出
    toastedJobs.delete(nj.id)
    job.value = nj
    upsertHistory(nj)
    void poll(nj.id)
  } catch (e) {
    message.error(`重试失败：${(e as Error).message}`)
  }
}

/* ---------------------------------------------------------------- 改名 */
const renaming = ref(false)
const renameDraft = ref('')

function startRename() {
  renameDraft.value = job.value?.title || ''
  renaming.value = true
}
async function commitRename() {
  renaming.value = false
  const j = job.value
  const title = renameDraft.value.trim()
  if (!j || !title || title === j.title) return
  try {
    const updated = await updateRenderJob(j.id, { title })
    job.value = updated
    upsertHistory(updated)
    message.success('已改名')
  } catch (e) {
    message.error(`改名失败：${(e as Error).message}`)
  }
}

async function onDelete(j: RenderJob) {
  try {
    await deleteRenderJob(j.id)
    message.success('已删除')
    if (job.value?.id === j.id) job.value = null
    await refreshList()
  } catch (e) {
    message.error(`删除失败：${(e as Error).message}`)
  }
}

/* ---------------------------------------------------------------- PPT */
const pptFiles = ref<UploadFileInfo[]>([])
const pptParsing = ref(false)

async function onPptUpload(options: { file: UploadFileInfo; fileList: UploadFileInfo[] }) {
  const raw = options.file.file
  // 无论成功失败都清空文件列表：NUpload max=1，不清掉下次就点不动了
  pptFiles.value = []
  if (!raw) return
  pptParsing.value = true
  try {
    const r = await parsePpt(raw)
    pptVisualScenes.value = r.visual_scenes
    pptTitle.value = r.title
    const lack = r.slides_count - (r.slide_images_ok ?? 0)
    message.success(
      `解析完成：${r.slides_count} 页 → ${r.chars} 字（约 ${r.estimate_seconds} 秒）` +
        (lack > 0 ? `，${lack} 页图片渲染失败将用文字卡片` : ''),
    )
  } catch (e) {
    message.error(`解析失败：${(e as Error).message}`)
  } finally {
    pptParsing.value = false
  }
}

/* ---------------------------------------------------------------- 生命周期 */
onMounted(() => {
  const cached = sessionStorage.getItem('cw_video_script')
  if (cached) {
    plainScript.value = cached
    sessionStorage.removeItem('cw_video_script')
  }
  void loadCloneVoices()
  void refreshList().then(() => {
    // 有草稿就不覆盖；没草稿且最近任务是已完成的，把它的稿子回显出来
    if (!plainScript.value && !pptVisualScenes.value && job.value?.status === 'done') {
      echoJob(job.value)
    }
  })
})

onBeforeUnmount(stopPoll)
</script>

<template>
  <div class="video-page">
    <NAlert type="info" :bordered="false" class="banner">
      全流程本地渲染：edge-tts 免费配音 + 本地绘制画面 + ffmpeg 合成，<b>不花外部 API 钱</b>。
      任务落库持久化，刷新页面进度和历史都在。
    </NAlert>

    <div class="two-col">
      <!-- 左：参数 -->
      <div class="left-col">
        <NCard size="small" title="脚本与参数">
          <NForm label-placement="top" size="small">
            <!-- PPT 模式：逐页显示该页的口播稿，改哪页算哪页 -->
            <template v-if="pptVisualScenes?.length">
              <NFormItem :label="`口播稿（按页 · 共 ${pptVisualScenes.length} 页）`">
                <div class="page-scripts">
                  <div v-for="(p, i) in pptVisualScenes" :key="i" class="page-block">
                    <div class="page-head">
                      <span class="page-no">第 {{ i + 1 }} 页</span>
                      <span class="page-title" :title="p.title">{{ p.title || '（无标题）' }}</span>
                    </div>
                    <!-- 这一页在视频里的样子 + PPT 要点；有真实幻灯片图优先用真图 -->
                    <div class="page-slide">
                      <img
                        v-if="p.slide_image"
                        class="slide-thumb"
                        :src="slideImgUrl(p.slide_image!)"
                        alt="幻灯片原图"
                        loading="lazy"
                        title="点击查看大图"
                        @click="openPreview(slideImgUrl(p.slide_image!), `第 ${i + 1} 页 · ${p.title || '幻灯片原图'}`)"
                      />
                      <img
                        v-else
                        class="slide-thumb"
                        :src="cardPreviewUrl(i, p)"
                        alt="画面卡片预览"
                        loading="lazy"
                        title="点击查看大图"
                        @click="openPreview(cardPreviewUrl(i, p), `第 ${i + 1} 页 · ${p.title || '画面卡片'}`)"
                      />
                      <ul class="slide-points">
                        <li v-for="(pt, k) in p.body" :key="k">{{ pt }}</li>
                        <li v-if="!p.body.length" class="empty">该页无文字要点</li>
                      </ul>
                    </div>
                    <NInput
                      v-model:value="p.narration"
                      type="textarea"
                      :autosize="{ minRows: 2, maxRows: 6 }"
                      placeholder="这一页要念的话（默认取演讲者备注）"
                    />
                  </div>
                </div>
              </NFormItem>
            </template>
            <NFormItem v-else label="口播稿">
              <NInput
                v-model:value="plainScript"
                type="textarea"
                :autosize="{ minRows: 9, maxRows: 18 }"
                placeholder="粘贴口播稿，或到「对话创作」让 AI 生成一版带钩子的脚本。&#10;建议：开头 3 秒抛结论，中间 1 个信息点，结尾一句行动号召。"
              />
            </NFormItem>

            <div class="meta-row">
              <span>字数 <b>{{ wordCount }}</b></span>
              <span>预计时长 <b>约 {{ estimateSeconds }} 秒</b></span>
              <span>约 <b>{{ Math.max(1, Math.round(estimateSeconds / 4)) }}</b> 个画面</span>
              <span v-if="pptVisualScenes?.length" class="hint" style="margin: 0">
                画面将按每页标题+要点出卡片
              </span>
            </div>

            <NFormItem label="画面比例">
              <div class="ratio-picker">
                <button
                  v-for="r in RATIOS"
                  :key="r.value"
                  type="button"
                  class="ratio-opt"
                  :class="{ on: ratio === r.value }"
                  @click="ratio = r.value"
                >
                  <span class="ratio-shape" :style="ratioShape(r.value)" />
                  <span>{{ r.label }}</span>
                </button>
              </div>
            </NFormItem>
            <div class="hint form-hint">
              {{ RATIOS.find((r) => r.value === ratio)?.desc }}
            </div>

            <NFormItem label="配音音色">
              <NSelect v-model:value="voice" :options="voiceOptions" size="small" />
            </NFormItem>
            <div v-if="voice.startsWith('clone_')" class="hint form-hint">
              正在使用<b>你自己的复刻音色</b>配音，本地合成，不花外部 API 钱。
            </div>

            <!-- 录音克隆：上传一段自己的录音，克隆成可复用的音色 -->
            <VoiceClonePanel v-model:voices="cloneVoices" @refresh="loadCloneVoices" />

            <NFormItem label="内嵌字幕">
              <NSwitch v-model:value="subtitle" size="small" />
              <span class="hint" style="margin-left: 10px">
                口播稿按句封装成软字幕轨（可开关、可导出），画面不受影响
              </span>
            </NFormItem>

            <NSpace>
              <NButton
                type="primary"
                :loading="submitting"
                :disabled="renderGate.blocked.value"
                @click="onSubmit"
              >
                {{
                  renderGate.blocked.value
                    ? '无法渲染'
                    : renderGate.left.value === null
                      ? '提交渲染'
                      : `提交渲染（剩 ${renderGate.left.value}）`
                }}
              </NButton>
              <NButton :disabled="!script.trim()" @click="plainScript = ''">清空</NButton>
              <NButton v-if="pptVisualScenes?.length" quaternary @click="pptVisualScenes = null">
                退出分页模式
              </NButton>
            </NSpace>
            <!-- 常驻说明而不是 tooltip：禁用按钮的 hover 提示多数浏览器不触发 -->
            <div v-if="renderGate.reason" class="hint gate-tip">⚠ {{ renderGate.reason }}</div>
          </NForm>
        </NCard>

        <NCard size="small" title="PPT 转视频" style="margin-top: 14px">
          <NUpload
            :file-list="pptFiles"
            accept=".pptx"
            :max="1"
            :show-file-list="false"
            :custom-request="() => {}"
            @change="onPptUpload"
          >
            <NButton size="small" :loading="pptParsing" :disabled="pptGate.blocked.value">
              {{ pptParsing ? '解析中…' : '上传 PPT，解析成口播稿' }}
            </NButton>
          </NUpload>
          <div v-if="pptGate.reason" class="hint gate-tip">⚠ {{ pptGate.reason }}</div>
          <div class="hint" style="margin-top: 10px">
            优先取每页<b>演讲者备注</b>（那才是要念的话），没有备注时拼接页面文字。
            解析后<b>每页的口播稿单独一块</b>显示在上方，改哪页算哪页，不是黑盒。
            画面<b>优先用 PPT 每页的真实页面</b>（LibreOffice 渲染）做运镜背景，
            渲染不出来才退回标题+要点的文字卡片。
          </div>
        </NCard>
      </div>

      <!-- 右：进度与历史 -->
      <div class="right-col">
        <NCard size="small" title="渲染进度">
          <div v-if="!job" class="empty-tip">
            还没有任务。填好脚本点「提交渲染」，或者上传 PPT。
          </div>

          <div v-else>
            <div class="job-head">
              <template v-if="renaming">
                <NInput
                  v-model:value="renameDraft"
                  size="tiny"
                  style="flex: 1"
                  @blur="commitRename"
                  @keyup.enter="commitRename"
                />
              </template>
              <template v-else>
                <div class="job-title" :title="job.title">{{ job.title }}</div>
                <NButton
                  text
                  size="tiny"
                  title="修改任务名称"
                  :disabled="job.status === 'running' || job.status === 'queued'"
                  @click="startRename"
                >
                  ✎
                </NButton>
              </template>
              <NTag size="small" :type="statusMeta[job.status].type" :bordered="false">
                {{ statusMeta[job.status].label }}
              </NTag>
            </div>
            <div class="job-sub hint">
              {{ job.ratio }} · {{ voiceLabel(job.voice) }}<template v-if="job.source === 'ppt'"> · PPT</template>
            </div>

            <NProgress
              v-if="job.status !== 'done'"
              type="line"
              :percentage="job.progress"
              :status="job.status === 'failed' ? 'error' : 'default'"
              indicator-placement="inside"
              style="margin: 14px 0"
            />
            <div v-if="job.status === 'running' || job.status === 'queued'" class="hint">
              正在跑：<b>{{ job.stage }}</b>（每 2 秒自动刷新）
            </div>

            <!-- 完成：预览播放器 -->
            <template v-if="job.status === 'done' && job.output_url">
              <div class="player" :class="{ portrait: job.width < job.height }">
                <video
                  :src="mediaUrl(job.output_url)"
                  controls
                  playsinline
                  preload="metadata"
                />
              </div>
              <div class="meta-row" style="margin: 10px 0 0">
                <span>时长 <b>{{ fmtDur(job.duration_sec) }}</b></span>
                <span>分辨率 <b>{{ job.width }}×{{ job.height }}</b></span>
                <span>大小 <b>{{ fmtSize(job.size_bytes) }}</b></span>
              </div>
            </template>

            <NAlert v-if="job.status === 'failed'" type="error" :bordered="false" style="margin-top: 10px">
              {{ job.error }}
            </NAlert>

            <!-- 注意：这里不能用 NSpace 包裹多个 v-if 按钮 —— naive-ui 的
                 NSpace 对含 v-if 的插槽会重复渲染 vnode（实测「停止刷新」
                 出现两个）。原生 flex 没这个问题。 -->
            <div class="job-actions">
              <ActionBtn
                v-if="job.status === 'done' && job.output_url"
                name="download"
                tip="下载成片"
                type="primary"
                size="small"
                tag="a"
                :href="mediaUrl(job.output_url)"
                :download="`${job.title || 'video'}.mp4`"
              />
              <ActionBtn
                v-if="job.status === 'failed' || job.status === 'done'"
                name="rotate"
                tip="重新渲染"
                size="small"
                @click="onRetry(job)"
              />
              <ActionBtn v-if="polling" name="stop" tip="停止刷新" size="small" @click="stopPoll" />
              <NPopconfirm @positive-click="onDelete(job)">
                <template #trigger>
                  <NButton size="small" quaternary circle type="error" title="删除">
                    <template #icon><IconSvg name="trash" /></template>
                  </NButton>
                </template>
                删除任务和成片文件？
              </NPopconfirm>
            </div>
          </div>
        </NCard>

        <NCard size="small" style="margin-top: 14px">
          <template #header>
            <div class="hist-head">
              <span>历史任务（共 {{ totalJobs }}）</span>
              <NButton size="tiny" quaternary @click="refreshList">刷新</NButton>
            </div>
          </template>

          <!-- 过滤：状态标签 + 标题搜索 -->
          <div class="hist-filter">
            <NInput
              v-model:value="filterKeyword"
              size="tiny"
              clearable
              placeholder="搜索任务名称"
              style="flex: 1; min-width: 120px"
              @input="onSearchInput"
            />
            <div class="filter-tabs">
              <button
                v-for="f in STATUS_FILTERS"
                :key="f.value"
                type="button"
                class="filter-tab"
                :class="{ on: filterStatus === f.value }"
                @click="
                  filterStatus = f.value as 'all' | JobStatus;
                  onFilterChange()
                "
              >
                {{ f.label }}
              </button>
            </div>
          </div>

          <div v-if="!history.length" class="empty-tip">
            {{ filterStatus === 'all' && !filterKeyword ? '暂无历史任务' : '没有符合条件的任务' }}
          </div>
          <div v-else class="hist-list">
            <div
              v-for="h in history"
              :key="h.id"
              class="hist-row"
              :class="{ active: job?.id === h.id }"
              @click="selectJob(h)"
            >
              <span class="dot" :data-s="h.status" />
              <div class="hist-main">
                <div class="hist-title">{{ h.title }}</div>
                <div class="hist-meta">
                  {{ fmtTime(h.created_at) }} · {{ h.ratio }} · {{ voiceLabel(h.voice) }}
                  <template v-if="h.status === 'done'"> · {{ fmtDur(h.duration_sec) }} · {{ fmtSize(h.size_bytes) }}</template>
                  <template v-else-if="h.status === 'running'"> · {{ h.stage }} {{ h.progress }}%</template>
                  <template v-else-if="h.status === 'failed'"> · {{ h.error.slice(0, 20) }}</template>
                  <template v-if="h.source === 'ppt'"> · PPT</template>
                </div>
              </div>
              <NTag
                size="tiny"
                :bordered="false"
                :type="statusMeta[h.status].type"
                class="hist-status"
              >
                {{ statusMeta[h.status].label }}
              </NTag>
            </div>
          </div>
          <div v-if="pageCount > 1" class="hist-pager">
            <NButton
              size="tiny"
              quaternary
              :disabled="page <= 1"
              @click="goPage(page - 1)"
            >
              上一页
            </NButton>
            <span class="pager-info">{{ page }} / {{ pageCount }}</span>
            <NButton
              size="tiny"
              quaternary
              :disabled="page >= pageCount"
              @click="goPage(page + 1)"
            >
              下一页
            </NButton>
          </div>
        </NCard>
      </div>
    </div>

    <!-- 图片大图预览 -->
    <NModal
      v-model:show="previewOpen"
      preset="card"
      :title="previewCaption"
      class="preview-modal"
      :bordered="false"
      style="max-width: 90vw"
    >
      <img class="preview-img" :src="previewSrc" alt="大图预览" />
    </NModal>
  </div>
</template>

<style scoped>
.banner {
  margin-bottom: 16px;
}
.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
  gap: 16px;
  align-items: start;
}
.left-col,
.right-col {
  min-width: 0;
}
.meta-row {
  display: flex;
  gap: 18px;
  font-size: 12.5px;
  color: var(--app-text-2);
  margin: 2px 0 16px;
  flex-wrap: wrap;
}
.meta-row b {
  color: var(--app-primary);
}
.hint {
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.7;
}
/* 表单项之间的补充提示行：上贴所属控件、下留足与下一个标签的间距 */
.form-hint {
  margin: -6px 0 16px;
}
.hint b {
  color: var(--app-text-2);
}

/* 权限/配额挡住时的常驻说明（红色，跟普通 hint 区分开） */
.gate-tip {
  color: #d03050;
  margin-top: 8px;
}
.empty-tip {
  font-size: 13px;
  color: var(--app-text-3);
  padding: 28px 0;
  text-align: center;
}
.job-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.job-title {
  font-size: 14px;
  font-weight: 500;
  line-height: 1.4;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.player {
  margin-top: 14px;
  border-radius: 10px;
  overflow: hidden;
  background: #000;
  max-height: 420px;
  display: flex;
  justify-content: center;
}
.player video {
  max-width: 100%;
  max-height: 420px;
  display: block;
}
.player.portrait video {
  max-width: 62%;
}
.hist-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.filter-tabs {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.filter-tab {
  padding: 3px 10px;
  border: 1px solid var(--app-border);
  border-radius: 999px;
  background: transparent;
  color: var(--app-text-2);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.filter-tab:hover {
  border-color: var(--app-primary-hover);
}
.filter-tab.on {
  border-color: var(--app-primary-hover);
  color: var(--app-primary-hover);
  background: var(--app-primary-a10);
}
.hist-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--app-surface-3);
}
.pager-info {
  font-size: 12px;
  color: var(--app-text-2);
}
.hist-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.hist-list {
  display: flex;
  flex-direction: column;
}
.hist-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 8px;
  border-radius: 8px;
  cursor: pointer;
  border-bottom: 1px solid var(--app-surface-3);
}
.hist-row:last-child {
  border-bottom: 0;
}
.hist-row:hover {
  background: var(--app-surface-3);
}
.hist-row.active {
  background: var(--app-surface-4);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--app-text-3);
}
.dot[data-s='done'] {
  background: var(--app-teal);
}
.dot[data-s='running'] {
  background: var(--app-primary);
  animation: pulse 1s ease-in-out infinite alternate;
}
.dot[data-s='failed'] {
  background: var(--app-red);
}
@keyframes pulse {
  to {
    opacity: 0.35;
    transform: scale(0.85);
  }
}
.hist-main {
  flex: 1;
  min-width: 0;
}
.hist-title {
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hist-meta {
  font-size: 11.5px;
  color: var(--app-text-3);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hist-status {
  flex-shrink: 0;
  font-weight: 500;
}
.job-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-top: 14px;
}
.ratio-picker {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.ratio-opt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: transparent;
  color: var(--app-text-2);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.ratio-opt:hover {
  border-color: var(--app-primary-hover);
}
.ratio-opt.on {
  border-color: var(--app-primary-hover);
  color: var(--app-primary-hover);
  background: var(--app-primary-a08);
}
.ratio-shape {
  display: inline-block;
  border: 1.5px solid currentColor;
  border-radius: 3px;
  flex-shrink: 0;
}
.page-scripts {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
  max-height: 320px;
  overflow-y: auto;
  padding-right: 4px;
}
.page-block {
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 8px 10px;
}
.page-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.page-slide {
  display: flex;
  gap: 10px;
  margin-bottom: 8px;
  align-items: flex-start;
}
.slide-thumb {
  width: 64px;
  border-radius: 6px;
  border: 1px solid var(--app-border);
  flex-shrink: 0;
  display: block;
  cursor: zoom-in;
  transition: transform 0.15s, border-color 0.15s;
}
.slide-thumb:hover {
  transform: scale(1.06);
  border-color: var(--app-primary);
}
.preview-img {
  display: block;
  max-width: 100%;
  max-height: 78vh;
  margin: 0 auto;
  border-radius: 8px;
}
.slide-points {
  margin: 0;
  padding-left: 16px;
  font-size: 12px;
  color: var(--app-text-2);
  line-height: 1.7;
  flex: 1;
  min-width: 0;
}
.slide-points li {
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.slide-points li.empty {
  color: var(--app-text-3);
  font-style: italic;
  list-style: none;
  margin-left: -16px;
}
.page-no {
  font-size: 12px;
  color: var(--app-primary);
  flex-shrink: 0;
}
.page-title {
  font-size: 12px;
  color: var(--app-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.job-sub {
  margin-top: 2px;
}
@media (max-width: 900px) {
  .two-col {
    grid-template-columns: minmax(0, 1fr);
  }
  .player.portrait video {
    max-width: 100%;
  }
}
</style>
