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
import { computed, ref, watch } from 'vue'
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
  type TraceReport,
  type HumanizeResult,
  type LevelOption,
  type ZhuqueResult,
} from '@/api/humanize'

const props = defineProps<{
  show: boolean
  contentId: string
  platform: string
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
const levels = ref<LevelOption[]>([])
const platformDefault = ref<Record<string, string>>({})
const level = ref('')
const rewriting = ref(false)
const rewriteStage = ref('')
const rewriteProgress = ref(0)
const result = ref<HumanizeResult | null>(null)
const applying = ref(false)
// 朱雀 AI 检测：外部模型、花额度，必须用户点按钮才发起（和本地体检的界线）
const zhuqueLoading = ref(false)
const zhuque = ref<ZhuqueResult | null>(null)
const reportLoading = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

const levelOptions = computed(() =>
  levels.value.map((l) => ({ label: `${l.label} · ${l.desc}`, value: l.id })),
)

/** 强度档默认按平台走（知乎轻度 / 公众号中度 / 小红书重度），但用户可覆盖。 */
watch(
  () => props.platform,
  (p) => {
    if (!level.value) level.value = platformDefault.value[p] || 'medium'
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
  result.value = null
  try {
    const [rep, lv] = await Promise.all([
      scanTrace({
        content_id: props.contentId,
        text: props.text || '',
        platform: props.platform,
      }),
      getHumanizeLevels(),
    ])
    report.value = rep
    levels.value = lv.levels
    platformDefault.value = lv.platform_default
    if (!level.value) level.value = lv.platform_default[props.platform] || 'medium'
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '体检失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  async (v) => {
    if (v) await load()
    else stopPoll()
  },
)

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function onRewrite() {
  if (!props.contentId) return
  rewriting.value = true
  rewriteStage.value = '正在提交…'
  rewriteProgress.value = 5
  try {
    const { task_id } = await submitHumanize({
      content_id: props.contentId,
      platform: props.platform,
      level: level.value,
    })
    pollTimer = setInterval(async () => {
      try {
        const job = await getHumanizeJob(task_id)
        rewriteProgress.value = job.progress
        rewriteStage.value = job.stage
        if (job.status === 'done') {
          stopPoll()
          rewriting.value = false
          result.value = job.result
          const gained = (job.result?.after_score ?? 0) - (job.result?.before_score ?? 0)
          // 分数没涨也要如实说，不能一律弹「改写成功」
          if (gained <= 0) message.warning('改写完成，但痕迹指数没有提升——建议手动再改几处')
          else message.success(`改写完成，人工痕迹指数 +${gained}`)
        } else if (job.status === 'error') {
          stopPoll()
          rewriting.value = false
          message.error(job.error || '改写失败')
        }
      } catch {
        /* 轮询失败不打断：下次 tick 继续 */
      }
    }, 1500)
  } catch (e: any) {
    rewriting.value = false
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
    zhuque.value = await zhuqueDetect({
      content_id: props.contentId,
      text: props.text || '',
    })
    if (!zhuque.value.ok) message.warning(zhuque.value.error || '朱雀检测失败')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '朱雀检测请求失败')
  } finally {
    zhuqueLoading.value = false
  }
}

/** 下载报告单：新窗口打开（可 Ctrl+P 存 PDF），被拦截则退化为文件下载。 */
async function onDownloadReport() {
  reportLoading.value = true
  try {
    const blob = await downloadDetectReport({
      content_id: props.contentId,
      text: props.text || '',
      platform: props.platform,
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
              />
              <IconTextBtn name="magic" type="primary" :loading="rewriting" @click="onRewrite">
                {{ rewriting ? '改写中' : 'AI 加痕迹' }}
              </IconTextBtn>
            </div>
            <div class="rewrite-hint">
              改写会消耗模型额度，且保留所有数字与事实（改完自动校验，丢了会报警）。
            </div>

            <div v-if="rewriting" class="rewrite-progress">
              <NProgress :percentage="rewriteProgress" :height="6" />
              <div class="stage">{{ rewriteStage }}</div>
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
.score-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.score-num {
  font-size: 48px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.score-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
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
.zq-report-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}
</style>
