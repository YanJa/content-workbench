<script setup lang="ts">
/**
 * 人工痕迹体检抽屉。
 *
 * 两件事，界线要守住：
 *   1. **体检**是零 LLM 的纯特征扫描，毫秒级、零成本，可以随便点；
 *   2. **加痕迹**要走模型，花钱，所以是异步任务 + 轮询，且必须用户点按钮才发起。
 *
 * 分数方向是「人工痕迹指数，越高越像人写的」——刻意不用市面通行的「AI 率」，
 * 那个越低越好的口径用户很容易看反，而我们要做的动作是「补痕迹」。
 *
 * 报告里那条 note 必须原样展示：这是特征扫描，不是「是否 AI 生成」的判定。
 * 检测器真实场景误判率 15-30%，给一个看起来精确的判定结论是害人。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  NDrawer,
  NDrawerContent,
  NAlert,
  NTag,
  NSelect,
  NSpin,
  NEmpty,
  NDivider,
  NProgress,
  NButton,
  NRadioGroup,
  NRadioButton,
  useMessage,
} from 'naive-ui'
import IconTextBtn from './IconTextBtn.vue'
import {
  scanTrace,
  submitHumanize,
  getHumanizeJob,
  applyHumanize,
  getHumanizeLevels,
  zhuqueDetect,
  downloadDetectReport,
  saveTraceSnapshot,
  type TraceReport,
  type HumanizeResult,
  type LevelOption,
  type TraceSource,
  type ZhuqueResult,
} from '@/api/humanize'

const props = defineProps<{
  show: boolean
  contentId: string
  platform: string
  /** 子形态（知乎：answer/article/idea；公众号：article/tietu）：决定档位默认值与口径 */
  subType?: string
  /** 直接传入的正文（面板里正在编辑的源文优先，避免读到库里的旧版本） */
  text?: string
}>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  /** 改写已写回源文：父组件要刷新面板，并提示重新排版 */
  (e: 'applied', text: string): void
}>()

const message = useMessage()

const loading = ref(false)
const report = ref<TraceReport | null>(null)
/**
 * 检哪一份：排版产物（默认）还是源文。
 *
 * 默认检排版产物的理由：真正发出去、被平台判 AIGC 的是**排版后那份纯文本**，
 * 源文只是中间稿。两者分数可能差一大截（实测同一条内容：源文 70、排版产物 86），
 * 拿源文分数判断「能不能发」会判断错。
 */
const source = ref<TraceSource>('layout')
/** 这次实际检的口径（后端回落时会和 source 不一致，要如实告诉用户） */
const actualSource = computed(() => report.value?.source || source.value)
/** 口径的中文名，界面统一从这里取，避免各处写死「源文/排版产物」写串 */
const srcLabel = (s: TraceSource) => (s === 'body' ? '源文' : '排版产物')
const levels = ref<LevelOption[]>([])
const platformDefault = ref<Record<string, string>>({})
const level = ref('')
const rewriting = ref(false)
const rewriteStage = ref('')
const rewriteProgress = ref(0)
/**
 * 模型调用阶段没有可信百分比，进度条该停住、由「已用 xx 秒」证明它还活着。
 * 之前只给一个静止的 55%，用户看到的就是「卡死」。
 */
const rewriteIndeterminate = ref(false)
const elapsedSec = ref(0)
const result = ref<HumanizeResult | null>(null)
const applying = ref(false)
// 朱雀 AI 检测：外部模型、花额度，必须用户点按钮才发起（和本地体检的界线）
const zhuqueLoading = ref(false)
/**
 * 朱雀结果按「检测对象」各存一份，切换开关不丢。
 *
 * 朱雀是一次花钱的外部调用，切个开关就清零等于逼用户重测一遍。
 * 但两份结果必须分开放 —— 混成一个就会出现「显示源文的结论、标的却是排版产物」。
 */
const zhuqueMap = ref<Partial<Record<TraceSource, ZhuqueResult>>>({})
/** 当前对象的那份结果（实际口径由后端回传，回落时也跟着对） */
const zhuque = computed<ZhuqueResult | null>(() => zhuqueMap.value[actualSource.value] || null)
const reportLoading = ref(false)
/** 进度区 DOM：起跑时滚进视口用（见 onRewrite） */
const progressBox = ref<HTMLElement | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
// 连续轮询失败次数：任务在内存里，服务一重启就 404，静默重试只会永远转圈
let pollFailures = 0

/** 已用时长：本地每秒自增，不依赖轮询（网络抖动时秒数也得继续走）。 */
const elapsedText = computed(() => {
  const s = elapsedSec.value
  if (s < 60) return `${s} 秒`
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`
})

const levelOptions = computed(() =>
  levels.value.map((l) => ({ label: `${l.label} · ${l.desc}`, value: l.id })),
)

/**
 * 档位默认 key：平台级（gzh）优先回落，子形态级（gzh_tietu）优先命中——
 * 与后端 PLATFORM_DEFAULT_LEVEL 的 key 结构一致（贴图默认 heavy）。
 */
const levelKey = computed(() =>
  props.subType ? `${props.platform}_${props.subType}` : props.platform,
)

/**
 * 强度档默认按平台/子形态走（知乎轻度 / 公号图文中度 / 贴图重度）。
 * 用户手动选过档就不再自动改（levelTouched），否则换形态时回到默认。
 */
const levelTouched = ref(false)
watch(
  levelKey,
  (k) => {
    if (!levelTouched.value) level.value = platformDefault.value[k] || 'medium'
  },
)

/**
 * 换了一条内容：档位偏好、上一次的改写结果、朱雀结果都不许串味。
 *
 * 朱雀那份尤其致命：它现在按对象缓存在组件里（切换对象不丢），
 * 换文章时若不清，B 文章的界面会挂着 A 文章的检测结论，
 * 而且标注还写着「检测对象：排版产物」—— 看起来完全像是 B 的。
 */
watch(
  () => props.contentId,
  () => {
    levelTouched.value = false
    level.value = ''
    result.value = null
    zhuqueMap.value = {}
  },
)

const scoreColor = computed(() => {
  const s = report.value?.score ?? 0
  if (s >= 80) return 'var(--app-success, #18a058)'
  if (s >= 60) return 'var(--app-warning, #f0a020)'
  return 'var(--app-error, #d03050)'
})

const severityType = (s: string) => (s === 'high' ? 'error' : s === 'mid' ? 'warning' : 'default')
const severityLabel = (s: string) => (s === 'high' ? '明显' : s === 'mid' ? '中等' : '轻微')

async function load() {
  loading.value = true
  try {
    const [rep, lv] = await Promise.all([
      scanTrace({
        content_id: props.contentId,
        // 切到「源文」时以面板里正在编辑的正文为准；排版产物只能读库
        text: source.value === 'body' ? props.text || '' : '',
        platform: props.platform,
        sub_type: props.subType || '',
        source: source.value,
      }),
      getHumanizeLevels(),
    ])
    report.value = rep
    levels.value = lv.levels
    platformDefault.value = lv.platform_default
    if (!levelTouched.value) {
      level.value = lv.platform_default[levelKey.value] || 'medium'
    }
    // 体检结果存成快照：已发布内容回看「当时的检测数据」用。零成本，随手存。
    if (rep.ok) void persistSnapshot(actualSource.value, rep, null)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '体检失败')
  } finally {
    loading.value = false
  }
}

/**
 * 把刚拿到的检测结果存成快照（检哪份存哪份），供已发布内容回看「当时的检测数据」。
 * 失败静默：快照只是辅助回看，绝不能因为存失败而打断体检/检测本身。
 */
async function persistSnapshot(
  sourceKey: TraceSource,
  scan?: TraceReport | null,
  zhuque?: ZhuqueResult | null,
) {
  if (!props.contentId || (!scan && !zhuque)) return
  try {
    await saveTraceSnapshot(props.contentId, {
      platform: props.platform,
      sub_type: props.subType || '',
      source: sourceKey,
      scan: scan ?? null,
      zhuque: zhuque ?? null,
    })
  } catch {
    /* 静默 */
  }
}

watch(
  () => props.show,
  async (v) => {
    if (!v) return
    await load()
    // 关掉抽屉**不停轮询**：任务还在模型那边跑，停下来只会让回来时看到一屏
    // 冻住的百分比。跑完照样弹「改写完成」，重新打开也还看得见结果。
    // 重新打开时抽屉滚回顶部，进度区又沉到视口外了 —— 再顶一次。
    if (rewriting.value) {
      nextTick(() => progressBox.value?.scrollIntoView({ block: 'center' }))
    }
  },
)

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
}

/** 收尾：停轮询、停计时、恢复按钮（三个出错分支都要走到这里，别各写一遍）。 */
function finishRewrite() {
  stopPoll()
  rewriting.value = false
  rewriteIndeterminate.value = false
}

async function onRewrite() {
  if (!props.contentId) return
  rewriting.value = true
  rewriteStage.value = '正在提交…'
  rewriteProgress.value = 5
  rewriteIndeterminate.value = false
  elapsedSec.value = 0
  pollFailures = 0
  // 抽屉内容可能很长（体检命中一堆），进度区在按钮下方，滚出视口就等于没反馈。
  // 起跑后把它顶进可视范围，用户的眼睛不用自己找。
  requestAnimationFrame(() => progressBox.value?.scrollIntoView({ block: 'center' }))
  try {
    const { task_id } = await submitHumanize({
      content_id: props.contentId,
      platform: props.platform,
      level: level.value,
      sub_type: props.subType || '',
    })
    // 秒表先跑起来：模型一次要两分钟，界面必须一直在动
    stopPoll()
    tickTimer = setInterval(() => {
      elapsedSec.value += 1
    }, 1000)
    pollTimer = setInterval(async () => {
      try {
        const job = await getHumanizeJob(task_id)
        pollFailures = 0
        rewriteProgress.value = job.progress
        rewriteStage.value = job.stage
        rewriteIndeterminate.value = !!job.indeterminate
        if (job.elapsed != null) elapsedSec.value = Math.max(elapsedSec.value, job.elapsed)
        if (job.status === 'done') {
          finishRewrite()
          result.value = job.result
          const gained = (job.result?.after_score ?? 0) - (job.result?.before_score ?? 0)
          // 分数没涨也要如实说，不能一律弹「改写成功」
          if (gained <= 0) message.warning('改写完成，但痕迹指数没有提升——建议手动再改几处')
          else message.success(`改写完成，人工痕迹指数 +${gained}`)
        } else if (job.status === 'error') {
          finishRewrite()
          message.error(job.error || '改写失败')
        }
      } catch (e: any) {
        // 404 = 任务已经不存在（服务重启会清空内存任务表）。继续重试只会
        // 让界面永远停在最后一个百分比上——那才是真正的「卡死」。
        if (e?.response?.status === 404) {
          finishRewrite()
          message.error('这次改写的任务已失效（后端可能重启过），请重新点一次「AI 加痕迹」')
          return
        }
        if (++pollFailures >= 5) {
          finishRewrite()
          message.error('查询改写进度连续失败，已停止等待。请检查后端是否在运行后重试')
        }
      }
    }, 1500)
  } catch (e: any) {
    finishRewrite()
    message.error(e?.response?.data?.detail || '提交改写失败')
  }
}

async function onApply() {
  if (!result.value?.text) return
  applying.value = true
  try {
    const r = await applyHumanize(props.contentId, result.value.text)
    message.success(`${r.message}（当前痕迹指数 ${r.score}）`)
    emit('applied', result.value.text)
    emit('update:show', false)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '写回失败')
  } finally {
    applying.value = false
  }
}

/** 朱雀 AIGC 值配色：与官方报告单一致（<0.5 人工 / 0.5–0.9 疑似 / ≥0.9 AI）。 */
const confColor = (c: number) =>
  c >= 0.9 ? 'var(--app-error, #d03050)' : c >= 0.5 ? 'var(--app-warning, #f0a020)' : 'var(--app-success, #18a058)'
const pct = (x: number) => `${(x * 100).toFixed(2)}%`

async function onZhuque() {
  zhuqueLoading.value = true
  try {
    const r = await zhuqueDetect({
      content_id: props.contentId,
      text: source.value === 'body' ? props.text || '' : '',
      platform: props.platform,
      source: source.value,
    })
    // 按后端回传的实际口径归档（请求 layout 但没排版时会回落成 body）
    const key: TraceSource = (r.source as TraceSource) || source.value
    zhuqueMap.value = { ...zhuqueMap.value, [key]: r }
    // 朱雀结果也存成快照（花钱的检测，落一份免得以后回看还要重测）
    if (r.ok) void persistSnapshot(key, null, r)
    if (!r.ok) message.warning(r.error || '朱雀检测失败')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '朱雀检测请求失败')
  } finally {
    zhuqueLoading.value = false
  }
}

/** 组件卸载（切页面/关标签）也要停掉计时器，否则定时器泄漏。 */
onBeforeUnmount(() => stopPoll())

/**
 * 切换检测对象：立刻重体检。
 * 朱雀结果**不清** —— 它按对象各存一份，切回来还在，不必重复花钱。
 */
watch(source, async () => {
  await load()
})

/** 体检对象没排版可用时后端会回落成源文，这时把开关也拨回去，免得开关和结果对不上。 */
watch(
  () => report.value?.source,
  (s) => {
    if (s && s !== source.value) source.value = s
  },
)

/** 下载报告单：新窗口打开（可 Ctrl+P 存 PDF），被拦截则退化为文件下载。 */
async function onDownloadReport() {
  reportLoading.value = true
  try {
    const blob = await downloadDetectReport({
      content_id: props.contentId,
      text: source.value === 'body' ? props.text || '' : '',
      platform: props.platform,
      sub_type: props.subType || '',
      source: source.value,
    })
    const url = URL.createObjectURL(new Blob([blob], { type: 'text/html' }))
    const w = window.open(url, '_blank')
    if (!w) {
      const a = document.createElement('a')
      a.href = url
      a.download = 'AI生成检测报告单.html'
      a.click()
    }
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '报告单生成失败')
  } finally {
    reportLoading.value = false
  }
}
</script>

<template>
  <NDrawer :show="show" width="min(560px, 92vw)" placement="right" @update:show="(v) => emit('update:show', v)">
    <NDrawerContent title="人工痕迹体检" closable>
      <div class="trace">
        <!-- 检测对象开关：默认检排版产物（真正发出去的那一份） -->
        <div class="src-row">
          <NRadioGroup v-model:value="source" size="small">
            <NRadioButton value="layout">排版产物</NRadioButton>
            <NRadioButton value="body">源文</NRadioButton>
          </NRadioGroup>
          <span class="src-hint">
            {{
              source === 'layout'
                ? '检的是排版后待发布的纯文本 —— 平台判 AIGC 看的是它'
                : '检的是未排版的源文（Markdown）—— 改写前后的基准'
            }}
          </span>
        </div>
        <NAlert
          v-if="report && report.ok && actualSource !== source"
          type="warning"
          :bordered="false"
          class="src-fallback"
        >
          这条内容还没排版，没有可检的排版产物，上面检的是<strong>源文</strong>。排版后再来才能看到发布版的分数。
        </NAlert>
        <NSpin :show="loading">
          <template v-if="report && report.ok">
            <!-- 分数：人工痕迹指数，越高越像人写的 -->
            <div class="score-row">
              <div class="score-num" :style="{ color: scoreColor }">{{ report.score }}</div>
              <div class="score-meta">
                <div class="score-title">人工痕迹指数（越高越像人写的）</div>
                <NTag size="small" :bordered="false" :type="severityType(report.level === 'natural' ? 'low' : 'mid')">
                  {{ report.level_label }}
                </NTag>
                <div v-if="!report.short_sample" class="score-stats">
                  {{ report.stats.chars }} 字 · {{ report.stats.sentences }} 句 ·
                  句长节奏 {{ report.stats.sent_len_cv }} · 短句占比
                  {{ Math.round((report.stats.short_sent_ratio || 0) * 100) }}%
                </div>
              </div>
            </div>

            <!-- 口径声明：必须展示，不能省 -->
            <NAlert type="info" :bordered="false" class="note">{{ report.note }}</NAlert>

            <NDivider title-placement="left">命中的特征</NDivider>
            <NEmpty v-if="!report.hits.length" description="没查出明显的机器味" size="small" />
            <div v-else class="hits">
              <div v-for="h in report.hits" :key="h.key" class="hit">
                <div class="hit-head">
                  <NTag size="small" :bordered="false" :type="severityType(h.severity)">
                    {{ severityLabel(h.severity) }}
                  </NTag>
                  <span class="hit-label">{{ h.label }}</span>
                </div>
                <ul v-if="h.samples.length" class="hit-samples">
                  <li v-for="(s, i) in h.samples" :key="i">{{ s }}</li>
                </ul>
                <div class="hit-how">→ {{ h.how }}</div>
              </div>
            </div>

            <template v-if="report.tips.length">
              <NDivider title-placement="left">这个平台的人味标准</NDivider>
              <ul class="tips">
                <li v-for="(t, i) in report.tips" :key="i">{{ t }}</li>
              </ul>
            </template>

            <!-- 朱雀 AI 检测：外部黑箱概率，与本地体检并列。花钱，所以按钮触发。 -->
            <NDivider title-placement="left">朱雀 AI 检测（腾讯云）</NDivider>
            <div class="zq-row">
              <NButton size="small" :loading="zhuqueLoading" @click="onZhuque">
                {{ zhuque && zhuque.ok ? '重新检测' : '开始检测' }}
              </NButton>
              <NTag v-if="zhuque && zhuque.ok" size="small" :bordered="false" type="info">
                检测对象：{{ srcLabel(actualSource) }}
              </NTag>
              <span class="zq-hint">
                外部模型，消耗 Makers 免费 token 额度；中文文本有固有误差，结果仅为辅助判断。
              </span>
            </div>
            <div v-if="zhuque && zhuque.ok" class="zq-result">
              <div class="zq-bar">
                <span class="zq-seg" :style="{ width: pct(zhuque.human_ratio), background: 'var(--app-success, #18a058)' }" />
                <span class="zq-seg" :style="{ width: pct(zhuque.suspect_ratio), background: 'var(--app-warning, #f0a020)' }" />
                <span class="zq-seg" :style="{ width: pct(zhuque.ai_ratio), background: 'var(--app-error, #d03050)' }" />
              </div>
              <div class="zq-legend">
                <span><i class="dot" style="background: var(--app-success, #18a058)" />人工特征 {{ pct(zhuque.human_ratio) }}</span>
                <span><i class="dot" style="background: var(--app-warning, #f0a020)" />疑似AI {{ pct(zhuque.suspect_ratio) }}</span>
                <span><i class="dot" style="background: var(--app-error, #d03050)" />AI特征 {{ pct(zhuque.ai_ratio) }}</span>
              </div>
              <div class="zq-segs">
                <div v-for="s in zhuque.segments" :key="s.order" class="zq-seg-row">
                  <span class="zq-seg-no">片段{{ s.order }}</span>
                  <span class="zq-seg-text">{{ s.text }}</span>
                  <span class="zq-seg-conf" :style="{ color: confColor(s.conf) }">{{ s.conf.toFixed(4) }}</span>
                </div>
              </div>
              <div class="zq-meta">
                整体置信度 {{ zhuque.softmax_confidence.toFixed(4) }} ·
                本次消耗 {{ zhuque.tokens_used ?? 0 }} tokens ·
                {{ zhuque.ms ?? 0 }}ms
              </div>
            </div>
            <NAlert v-else-if="zhuque && !zhuque.ok" type="warning" :bordered="false" class="zq-err">
              {{ zhuque.error }}
            </NAlert>
            <div v-else class="zq-hint zq-empty">
              这一份（{{ srcLabel(actualSource) }}）还没做过朱雀检测。两份结果各自保留，切回去就能看到，不用重测。
            </div>
            <div class="zq-report-row">
              <NButton size="small" :loading="reportLoading" @click="onDownloadReport">
                下载报告单（HTML）
              </NButton>
              <span class="zq-hint">朱雀同款版式 + 本地体检合并；浏览器打开后 Ctrl+P 可存 PDF。</span>
            </div>

            <NDivider title-placement="left">加人工痕迹</NDivider>
            <div class="rewrite-row">
              <NSelect
                v-model:value="level"
                :options="levelOptions"
                size="small"
                style="flex: 1"
                placeholder="改写强度"
                @update:value="levelTouched = true"
              />
              <IconTextBtn name="magic" type="primary" :loading="rewriting" @click="onRewrite">
                {{ rewriting ? '改写中' : 'AI 加痕迹' }}
              </IconTextBtn>
            </div>
            <div class="rewrite-hint">
              改写会消耗模型额度，且保留所有数字与事实（改完自动校验，丢了会报警）。
            </div>

            <div v-if="rewriting" ref="progressBox" class="rewrite-progress">
              <NProgress
                :percentage="rewriteProgress"
                :height="6"
                :processing="rewriteIndeterminate"
                :show-indicator="!rewriteIndeterminate"
              />
              <div class="stage">
                {{ rewriteStage }}
                <span class="elapsed">已用 {{ elapsedText }}</span>
              </div>
              <div v-if="rewriteIndeterminate" class="rewrite-hint">
                推理模型会先想再写，一篇千字稿通常要 1–3 分钟。这段时间没有新进度是正常的，
                别重复点——重复提交只会在队列里再排一轮。
              </div>
            </div>

            <template v-if="result">
              <NDivider title-placement="left">改写结果</NDivider>
              <div class="cmp">
                <span class="cmp-old">{{ result.before_score }}</span>
                <span class="cmp-arrow">→</span>
                <span class="cmp-new" :style="{ color: scoreColor }">{{ result.after_score }}</span>
                <span class="cmp-label">人工痕迹指数</span>
              </div>
              <NAlert
                v-if="result.warnings.length"
                type="warning"
                :bordered="false"
                class="warn"
              >
                <div v-for="(w, i) in result.warnings" :key="i">{{ w }}</div>
                <div class="warn-tip">数字被改坏了就别用这份，回退重新改或手动补回。</div>
              </NAlert>
              <div class="result-text">{{ result.text }}</div>
              <div class="apply-row">
                <NButton size="small" @click="result = null">放弃</NButton>
                <IconTextBtn name="save" type="primary" :loading="applying" @click="onApply">
                  用这份替换源文
                </IconTextBtn>
              </div>
              <div class="rewrite-hint">
                替换的是源文，排版产物会失效，需要重新排版一次（不会自动重排）。
              </div>
            </template>
          </template>
          <NEmpty v-else-if="report" :description="report.error || '体检失败'" size="small" />
        </NSpin>
      </div>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.trace {
  padding-bottom: 24px;
}
.src-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.src-hint {
  font-size: 12px;
  opacity: 0.6;
  flex: 1;
  min-width: 180px;
}
.src-fallback {
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.6;
}
.score-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.score-num {
  /* 绝对不能折行：数字之间是可断行点，窄屏下一被挤就把「86」拆成上下两个数字。
     flex:none 保住它自己的宽度，nowrap 兜住多位数（100 分时更明显）。 */
  flex: none;
  white-space: nowrap;
  font-size: 48px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.score-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  /* 允许它收缩换行，但不能把数字挤掉 */
  flex: 1;
  min-width: 0;
}
.score-title {
  font-weight: 600;
}
.score-stats {
  font-size: 12px;
  opacity: 0.65;
}
.note {
  margin-top: 12px;
  font-size: 12px;
  line-height: 1.6;
}
.hits {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hit {
  padding: 10px 12px;
  border: 1px solid var(--app-border, #e5e5e5);
  border-radius: 8px;
}
.hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hit-label {
  font-weight: 600;
  font-size: 13px;
}
.hit-samples {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  opacity: 0.75;
  line-height: 1.7;
}
.hit-how {
  margin-top: 6px;
  font-size: 12px;
  color: var(--app-primary, #18a058);
}
.tips {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.8;
  opacity: 0.8;
}
.rewrite-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rewrite-hint {
  margin-top: 8px;
  font-size: 12px;
  opacity: 0.6;
  line-height: 1.6;
}
.rewrite-progress {
  margin-top: 12px;
}
.stage {
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.7;
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.elapsed {
  flex: none;
  font-variant-numeric: tabular-nums;
  opacity: 0.9;
}
.cmp {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-variant-numeric: tabular-nums;
}
.cmp-old {
  font-size: 20px;
  opacity: 0.5;
}
.cmp-new {
  font-size: 28px;
  font-weight: 700;
}
.cmp-label {
  font-size: 12px;
  opacity: 0.6;
}
.warn {
  margin-top: 10px;
  font-size: 12px;
}
.warn-tip {
  margin-top: 4px;
  opacity: 0.8;
}
.result-text {
  margin-top: 10px;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.8;
  padding: 10px 12px;
  border: 1px solid var(--app-border, #e5e5e5);
  border-radius: 8px;
  background: var(--app-bg-soft, rgba(0, 0, 0, 0.02));
}
.apply-row {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}
.zq-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.zq-hint {
  font-size: 12px;
  opacity: 0.6;
  line-height: 1.6;
  line-height: 1.5;
}
.zq-result {
  margin-top: 12px;
}
.zq-bar {
  display: flex;
  width: 100%;
  height: 18px;
  border-radius: 6px;
  overflow: hidden;
}
.zq-seg {
  height: 100%;
  min-width: 0;
  transition: width 0.3s;
}
.zq-legend {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.8;
}
.zq-legend .dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 3px;
  margin-right: 4px;
}
.zq-segs {
  margin-top: 10px;
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--app-border, #e5e5e5);
  border-radius: 8px;
}
.zq-seg-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.6;
}
.zq-seg-row + .zq-seg-row {
  border-top: 1px solid var(--app-border, #e5e5e5);
}
.zq-seg-no {
  flex: none;
  opacity: 0.55;
  font-variant-numeric: tabular-nums;
}
.zq-seg-text {
  flex: 1;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.zq-seg-conf {
  flex: none;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.zq-meta {
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.55;
  font-variant-numeric: tabular-nums;
}
.zq-err {
  margin-top: 10px;
  font-size: 12px;
}
.zq-empty {
  flex: 1 0 100%;
  margin-top: 8px;
}
.zq-report-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}

/* 窄屏（手机）：抽屉只有 92vw，48px 的分数 + 说明挤成两行会把「86」竖着拆开。
   缩字号、收紧间距，让分数和说明各有各的地方。 */
@media (max-width: 480px) {
  .score-row {
    gap: 12px;
    align-items: flex-start;
  }
  .score-num {
    font-size: 38px;
  }
  .score-title {
    font-size: 14px;
    line-height: 1.4;
  }
  .score-stats {
    line-height: 1.5;
  }
  .src-hint {
    flex: 1 0 100%;
  }
}
</style>
