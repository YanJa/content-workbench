<script setup lang="ts">
/**
 * AI 用量页。
 *
 * 这一页要回答三个问题：**花了多少钱、花在哪儿、能不能省**。
 * 所以除了总量，还必须把「缓存命中」单独拆出来 —— 命中的输入只有未命中的
 * 1/50，是唯一的省钱杠杆，混在总数里就等于没有。
 *
 * 图表全部手写 SVG：
 *   不引第三方图表库，一是这个页面只需要柱状图和条形图，引 ECharts 为了两张图
 *   多 300KB 不划算；二是自绘能完全控制暗色主题下的配色和刻度线对比度。
 */
import { computed, h, onMounted, ref, watch } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NSelect,
  NSpace,
  NSpin,
  NTag,
  NTooltip,
  type DataTableColumns,
} from 'naive-ui'

import * as usageApi from '@/api/usage'
import IconTextBtn from '@/components/IconTextBtn.vue'
import type {
  BillingKind,
  PriceRow,
  TopConversation,
  UsageRecord,
  UsageSeriesItem,
} from '@/api/usage'

// ------------------------------------------------------------------ 状态
const loading = ref(true)
const err = ref('')
const summary = ref<usageApi.UsageSummary | null>(null)
const records = ref<UsageRecord[]>([])
const recordsTotal = ref(0)
const topConvs = ref<TopConversation[]>([])
const pricing = ref<PriceRow[]>([])
const pricingState = ref<Record<string, any>>({})

const RANGES = [
  { label: '近 7 天', value: 7 },
  { label: '近 30 天', value: 30 },
  { label: '近 90 天', value: 90 },
  { label: '全部', value: 0 },
]
const days = ref(30)
const page = ref(0)
const PAGE_SIZE = 20

// SVG 的 presentation attribute（fill="..."）不支持 var()，
// 所以图表颜色统一走 :style 绑定，值用 CSS 变量 —— 主题切换自动跟随。
const COLORS: Record<string, string> = {
  in: 'var(--app-primary)',
  in_cached: 'var(--app-teal)',
  out: 'var(--app-amber)',
}

const SCENE_LABEL: Record<string, string> = {
  gzh: '公众号',
  xiaohongshu: '小红书',
  douyin: '抖音',
  zhihu: '知乎',
  toutiao: '今日头条',
  baijiahao: '百家号',
  chat: '通用对话',
  summary: '历史压缩',
  layout: '排版装配',
  humanize: '痕迹改写',
  imagegen: '配图',
}

// ------------------------------------------------------------------ 格式化
function fmtMoney(v: number): string {
  if (!v) return '¥0'
  return v < 1 ? `¥${v.toFixed(4)}` : `¥${v.toFixed(2)}`
}
function fmtTokens(v: number): string {
  if (!v) return '0'
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)} 亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(1)} 万`
  return v.toLocaleString('zh-CN')
}
function fmtAxis(v: number): string {
  if (v >= 10_000) return `${(v / 10_000).toFixed(v >= 100_000 ? 0 : 1)}万`
  if (v >= 1_000) return `${(v / 1_000).toFixed(0)}k`
  return String(Math.round(v))
}
function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
function fmtDuration(ms: number): string {
  if (!ms) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

/** 把 Y 轴最大值抬到一个好看的整数，否则刻度会出现 8734 这种鬼数字 */
function niceMax(v: number): number {
  if (v <= 0) return 1
  const exp = Math.floor(Math.log10(v))
  const base = 10 ** exp
  const n = v / base
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * base
}

// ------------------------------------------------------------------ 取数
async function refresh() {
  loading.value = true
  err.value = ''
  try {
    const params = { days: days.value || undefined }
    const [s, r, t, p] = await Promise.all([
      usageApi.usageSummary(params),
      usageApi.usageRecords({ ...params, limit: PAGE_SIZE, offset: page.value * PAGE_SIZE }),
      usageApi.usageTopConversations(days.value || null, 8),
      usageApi.usagePricing(),
    ])
    summary.value = s
    records.value = r.items
    recordsTotal.value = r.total
    topConvs.value = t
    pricing.value = p.items
    pricingState.value = p.state
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
watch(days, () => {
  page.value = 0
  refresh()
})
watch(page, refresh)

// ------------------------------------------------------------------ 概览
const total = computed(() => summary.value?.total ?? null)

const cacheRate = computed(() => {
  const t = total.value
  if (!t || !t.prompt_tokens) return 0
  return (t.cached_tokens / t.prompt_tokens) * 100
})

/** 缓存命中省了多少钱：按"这些 token 如果全都没命中"反推差额 */
const cacheSaved = computed(() => {
  const t = total.value
  if (!t || !t.cached_tokens) return 0
  const per = pricing.value.find((p) => p.model === 'deepseek-flash')
  if (!per) return 0
  const unit = per.idle.in - (per.idle.in_cached ?? per.idle.in)
  // 高峰时段命中的省得更多，这里用 1.5 倍粗略近似（页面只做量级参考）
  return (t.cached_tokens * unit * 1.5) / 1_000_000
})

// ------------------------------------------------------------------ 趋势图
/** 后端只返回"有数据的天"，这里补成连续日期，否则趋势图看着像跳变 */
const filledDays = computed<UsageSeriesItem[]>(() => {
  const src = summary.value?.by_day ?? []
  if (src.length < 2) return src
  const first = new Date(`${src[0].key}T00:00:00`)
  const last = new Date(`${src[src.length - 1].key}T00:00:00`)
  const span = Math.round((last.getTime() - first.getTime()) / 86400000)
  if (span > 180) return src // 跨度太大就别补了，补出一两千根柱子没法看

  const map = new Map(src.map((d) => [d.key, d]))
  const out: UsageSeriesItem[] = []
  for (let i = 0; i <= span; i++) {
    const d = new Date(first.getTime() + i * 86400000)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
      d.getDate(),
    ).padStart(2, '0')}`
    out.push(
      map.get(key) ?? {
        key,
        calls: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        reasoning_tokens: 0,
        cached_tokens: 0,
        total_tokens: 0,
        cost: 0,
        avg_latency_ms: 0,
      },
    )
  }
  return out
})

const chart = computed(() => {
  const data = filledDays.value
  const W = 760
  const H = 230
  const padL = 52
  const padR = 14
  const padT = 14
  const padB = 28
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const n = Math.max(data.length, 1)
  const slot = plotW / n
  const barW = Math.min(slot * 0.62, 30)
  const baseY = padT + plotH
  const yMax = niceMax(Math.max(...data.map((d) => d.total_tokens), 1))

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => ({
    value: yMax * t,
    y: baseY - plotH * t,
  }))

  const bars = data.map((d, i) => {
    const cached = Math.min(d.cached_tokens, d.prompt_tokens)
    const miss = d.prompt_tokens - cached
    const x = padL + slot * i + (slot - barW) / 2
    const hMiss = (miss / yMax) * plotH
    const hCached = (cached / yMax) * plotH
    const hOut = (d.completion_tokens / yMax) * plotH
    return {
      key: d.key,
      x,
      w: barW,
      total: d.total_tokens,
      calls: d.calls,
      cost: d.cost,
      segs: [
        { key: 'in', y: baseY - hMiss, h: hMiss },
        { key: 'in_cached', y: baseY - hMiss - hCached, h: hCached },
        { key: 'out', y: baseY - hMiss - hCached - hOut, h: hOut },
      ],
    }
  })

  // X 轴标签：天数多了就抽稀，不然全糊在一起
  const step = Math.max(1, Math.ceil(n / 10))
  const xLabels = data
    .map((d, i) => ({ key: d.key, i }))
    .filter(({ i }) => i % step === 0 || i === n - 1)
    .map(({ key, i }) => ({ text: key.slice(5), x: padL + slot * i + slot / 2 }))

  return { W, H, padL, plotW, baseY, ticks, bars, xLabels }
})

/** 费用趋势：独立迷你面积图，不跟 token 柱共用一个 Y 轴 ——
 *  共轴会把「钱」那条线压成一条直线（token 量级大几个数量级），看不出波动。 */
const costChart = computed(() => {
  const data = filledDays.value
  const W = 760
  const H = 88
  const padL = 52
  const padR = 14
  const padT = 10
  const padB = 18
  const plotW = W - padL - padR
  const plotH = H - padT - padB
  const n = Math.max(data.length, 1)
  const slot = plotW / n
  const max = niceMax(Math.max(...data.map((d) => d.cost), 0.0001))
  const pts = data.map((d, i) => ({
    x: padL + slot * i + slot / 2,
    y: padT + plotH - (d.cost / max) * plotH,
  }))
  const line = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
  const area = `${padL},${padT + plotH} ${line} ${padL + plotW},${padT + plotH}`
  return { W, H, padL, padT, plotW, plotH, max, pts, line, area }
})

// ------------------------------------------------------------------ 分组条形
function barRows(items: UsageSeriesItem[]) {
  const max = Math.max(...items.map((d) => d.cost), 0.0001)
  const rowH = 30
  const labelW = 108
  const valueW = 84
  const barMax = 400 - labelW - valueW - 8
  return {
    W: 400,
    H: Math.max(items.length * rowH, 30),
    labelW,
    barMax,
    rows: [...items]
      .sort((a, b) => b.cost - a.cost)
      .map((it, i) => ({
        key: it.key,
        label: it.label || it.key,
        known: it.known !== false,
        value: it.cost,
        y: i * rowH,
        labelW,
        barMax,
        barW: Math.max((it.cost / max) * barMax, 2),
      })),
  }
}

const modelBars = computed(() => barRows(summary.value?.by_model ?? []))
const sceneBars = computed(() => barRows(summary.value?.by_scene ?? []))

/** 单价表里没登记、却被实际调用过的模型 —— 这些数字是兜底估算，必须提示 */
const unknownModels = computed(() =>
  (summary.value?.by_model ?? []).filter((m) => (m.billing ?? 'unknown') === 'unknown'),
)

/** 这条记录算哪种账。老数据没有 billing 字段，用 price_known 兜底 */
function billingOf(r: UsageRecord): BillingKind {
  if (r.billing) return r.billing
  return r.price_known ? 'paid' : 'unknown'
}

/** 免费调用：不花钱，但吃的是每日额度，得让人看得见 */
const freeStat = computed(() => {
  const t = summary.value?.total
  return { calls: t?.free_calls ?? 0, tokens: t?.free_tokens ?? 0 }
})

// ------------------------------------------------------------------ 单价表派生
const hasCacheColumn = computed(() => pricing.value.some((p) => p.has_cache_price))
function hasPeak(p: PriceRow) {
  return (p.peak.in ?? 0) !== (p.idle.in ?? 0)
}
function peakMul(p: PriceRow) {
  return p.idle.in ? Math.round((p.peak.in / p.idle.in) * 100) / 100 : 1
}

// ------------------------------------------------------------------ 明细表
const columns: DataTableColumns<UsageRecord> = [
  { title: '时间', key: 'created_at', width: 118, render: (r) => fmtTime(r.created_at) },
  { title: '场景', key: 'scene', width: 88, render: (r) => SCENE_LABEL[r.scene] ?? r.scene },
  {
    title: '模型',
    key: 'model',
    width: 160,
    render: (r) =>
      h(NSpace, { size: 4, wrap: false, align: 'center' }, () => [
        h('span', r.model_label || r.model),
        // 免费：明确标出来，别让人看着 ¥0.00 以为漏记了；
        // 未知：标 ⚠，说明这笔是兜底估算，不是精确账单。
        billingOf(r) === 'free'
          ? h(NTag, { size: 'tiny', bordered: false, type: 'success' }, { default: () => '免费' })
          : billingOf(r) === 'unknown'
            ? h(NTag, { size: 'tiny', bordered: false, type: 'warning' }, { default: () => '⚠ 估价' })
            : null,
      ]),
  },
  {
    title: '输入',
    key: 'prompt_tokens',
    width: 140,
    render: (r) =>
      r.cached_tokens
        ? `${fmtTokens(r.prompt_tokens)}（缓存 ${fmtTokens(r.cached_tokens)}）`
        : fmtTokens(r.prompt_tokens),
  },
  {
    title: '输出',
    key: 'completion_tokens',
    width: 80,
    render: (r) => fmtTokens(r.completion_tokens),
  },
  {
    title: '思维链',
    key: 'reasoning_tokens',
    width: 80,
    render: (r) => (r.reasoning_tokens ? fmtTokens(r.reasoning_tokens) : '—'),
  },
  { title: '耗时', key: 'latency_ms', width: 74, render: (r) => fmtDuration(r.latency_ms) },
  {
    title: '时段',
    key: 'is_peak',
    width: 74,
    render: (r) =>
      h(
        NTag,
        { size: 'small', bordered: false, type: r.is_peak ? 'warning' : 'success' },
        { default: () => (r.is_peak ? '高峰' : '低谷') },
      ),
  },
  {
    title: '费用',
    key: 'cost',
    width: 92,
    render: (r) =>
      billingOf(r) === 'free'
        ? h('span', { style: 'font-size:12px;opacity:.6' }, '免费')
        : fmtMoney(r.cost),
  },
  {
    title: '来源',
    key: 'estimated',
    width: 100,
    render: (r) =>
      h(
        NTag,
        { size: 'small', bordered: false, type: r.estimated ? 'default' : 'info' },
        { default: () => (r.is_summary ? '历史压缩' : r.estimated ? '回填估算' : '实时埋点') },
      ),
  },
]

const pageCount = computed(() => Math.max(1, Math.ceil(recordsTotal.value / PAGE_SIZE)))
</script>

<template>
  <div class="usage">
    <div class="head">
      <div>
        <h2>AI 用量</h2>
        <p class="sub">花了多少钱、花在哪儿、哪部分还能省</p>
      </div>
      <div class="head-right">
        <NSelect v-model:value="days" :options="RANGES" size="small" class="range" />
        <IconTextBtn name="reset" size="small" quaternary @click="refresh">刷新</IconTextBtn>
      </div>
    </div>

    <NAlert v-if="err" type="error" :show-icon="false" class="err">{{ err }}</NAlert>

    <NSpin :show="loading">
      <template v-if="total && total.calls">
        <!-- KPI -->
        <div class="kpis">
          <div class="kpi accent">
            <span class="k-label">总费用</span>
            <strong class="k-value">{{ fmtMoney(total.cost) }}</strong>
            <span class="k-foot">{{ total.calls }} 次调用</span>
            <span v-if="freeStat.calls" class="k-foot free-note">
              含 {{ freeStat.calls }} 次免费模型（{{ fmtTokens(freeStat.tokens) }} tokens，不花钱但占额度）
            </span>
          </div>
          <div class="kpi">
            <span class="k-label">总 tokens</span>
            <strong class="k-value">{{ fmtTokens(total.total_tokens) }}</strong>
            <span class="k-foot">
              入 {{ fmtTokens(total.prompt_tokens) }} · 出 {{ fmtTokens(total.completion_tokens) }}
            </span>
          </div>
          <div class="kpi">
            <span class="k-label">缓存命中率</span>
            <strong class="k-value">{{ cacheRate.toFixed(1) }}%</strong>
            <span class="k-foot">已省约 {{ fmtMoney(cacheSaved) }}</span>
          </div>
          <div class="kpi">
            <span class="k-label">平均耗时</span>
            <strong class="k-value">{{ fmtDuration(total.avg_latency_ms) }}</strong>
            <span class="k-foot">思维链 {{ fmtTokens(total.reasoning_tokens) }}</span>
          </div>
        </div>

        <!-- 趋势 -->
        <NCard size="small" class="card">
          <template #header>
            <div class="card-head">
              <span>每日 token 消耗</span>
              <div class="legend">
                <i style="background: var(--app-primary)" />输入·未命中
                <i style="background: var(--app-teal)" />输入·缓存命中
                <i style="background: var(--app-amber)" />输出
              </div>
            </div>
          </template>

          <svg
            class="chart"
            :viewBox="`0 0 ${chart.W} ${chart.H}`"
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label="每日 token 消耗堆叠柱状图"
          >
            <g class="grid">
              <line
                v-for="t in chart.ticks"
                :key="t.value"
                :x1="chart.padL"
                :x2="chart.padL + chart.plotW"
                :y1="t.y"
                :y2="t.y"
              />
            </g>
            <g class="axis-y">
              <text
                v-for="t in chart.ticks"
                :key="t.value"
                :x="chart.padL - 8"
                :y="t.y + 4"
                text-anchor="end"
              >
                {{ fmtAxis(t.value) }}
              </text>
            </g>

            <g v-for="b in chart.bars" :key="b.key">
              <title>
                {{ b.key }}｜共 {{ fmtTokens(b.total) }} tokens｜{{ b.calls }} 次｜{{
                  fmtMoney(b.cost)
                }}
              </title>
              <rect
                v-for="s in b.segs"
                :key="s.key"
                :x="b.x"
                :y="s.y"
                :width="b.w"
                :height="Math.max(s.h, 0)"
                :style="{ fill: COLORS[s.key] }"
                rx="1.5"
              />
            </g>

            <g class="axis-x">
              <text
                v-for="(l, i) in chart.xLabels"
                :key="i"
                :x="l.x"
                :y="chart.H - 8"
                text-anchor="middle"
              >
                {{ l.text }}
              </text>
            </g>
          </svg>

          <div class="cost-title">每日费用（元）</div>
          <svg
            class="chart cost"
            :viewBox="`0 0 ${costChart.W} ${costChart.H}`"
            preserveAspectRatio="xMidYMid meet"
            role="img"
            aria-label="每日费用趋势"
          >
            <defs>
              <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                <stop
                  offset="0%"
                  :style="{ stopColor: 'var(--app-purple-soft)' }"
                  stop-opacity="0.45"
                />
                <stop
                  offset="100%"
                  :style="{ stopColor: 'var(--app-purple-soft)' }"
                  stop-opacity="0.02"
                />
              </linearGradient>
            </defs>
            <line
              :x1="costChart.padL"
              :x2="costChart.padL + costChart.plotW"
              :y1="costChart.padT + costChart.plotH"
              :y2="costChart.padT + costChart.plotH"
              class="grid"
            />
            <polygon :points="costChart.area" fill="url(#costFill)" />
            <polyline
              :points="costChart.line"
              fill="none"
              :style="{ stroke: 'var(--app-purple-soft)' }"
              stroke-width="1.8"
            />
            <circle
              v-for="(p, i) in costChart.pts"
              :key="i"
              :cx="p.x"
              :cy="p.y"
              r="1.8"
              :style="{ fill: 'var(--app-purple-soft)' }"
            />
            <text
              :x="costChart.padL - 8"
              :y="costChart.padT + 8"
              text-anchor="end"
              class="axis-y"
            >
              {{ fmtMoney(costChart.max) }}
            </text>
          </svg>
        </NCard>

        <!-- 分组 -->
        <div class="grid2">
          <NCard size="small" class="card">
            <template #header>按模型（元）</template>
            <svg
              v-if="modelBars.rows.length"
              class="chart"
              :viewBox="`0 0 ${modelBars.W} ${modelBars.H}`"
              preserveAspectRatio="xMinYMin meet"
            >
              <g v-for="r in modelBars.rows" :key="r.key">
                <text :x="0" :y="r.y + 19" class="bar-label">
                  {{ r.label }}{{ r.known ? '' : ' ⚠' }}
                </text>
                <rect
                  :x="r.labelW"
                  :y="r.y + 6"
                  :width="r.barMax"
                  height="14"
                  rx="4"
                  class="bar-bg"
                />
                <rect
                  :x="r.labelW"
                  :y="r.y + 6"
                  :width="r.barW"
                  height="14"
                  rx="4"
                  :style="{
                    fill: r.known ? 'var(--app-primary)' : 'var(--app-text-2)',
                  }"
                />
                <text :x="r.labelW + r.barMax + 6" :y="r.y + 19" class="bar-value">
                  {{ fmtMoney(r.value) }}
                </text>
              </g>
            </svg>
            <NEmpty v-else description="暂无数据" size="small" />
          </NCard>

          <NCard size="small" class="card">
            <template #header>按场景（元）</template>
            <svg
              v-if="sceneBars.rows.length"
              class="chart"
              :viewBox="`0 0 ${sceneBars.W} ${sceneBars.H}`"
              preserveAspectRatio="xMinYMin meet"
            >
              <g v-for="r in sceneBars.rows" :key="r.key">
                <text :x="0" :y="r.y + 19" class="bar-label">
                  {{ SCENE_LABEL[r.key] ?? r.key }}
                </text>
                <rect
                  :x="r.labelW"
                  :y="r.y + 6"
                  :width="r.barMax"
                  height="14"
                  rx="4"
                  class="bar-bg"
                />
                <rect
                  :x="r.labelW"
                  :y="r.y + 6"
                  :width="r.barW"
                  height="14"
                  rx="4"
                  :style="{ fill: 'var(--app-teal)' }"
                />
                <text :x="r.labelW + r.barMax + 6" :y="r.y + 19" class="bar-value">
                  {{ fmtMoney(r.value) }}
                </text>
              </g>
            </svg>
            <NEmpty v-else description="暂无数据" size="small" />
          </NCard>
        </div>

        <NAlert v-if="unknownModels.length" type="warning" :show-icon="false" class="warn">
          以下模型还没登记单价，暂按 <b>{{ pricingState.default_model }}</b> 估算：
          {{ unknownModels.map((m) => m.key).join('、') }}。在
          <code>backend/config/pricing.json</code> 里补一条即可，改完调
          <code>POST /api/usage/pricing/reload</code>，不用重启。
        </NAlert>

        <!-- Top 会话 -->
        <NCard v-if="topConvs.length" size="small" class="card">
          <template #header>消耗最高的会话</template>
          <div class="top-list">
            <div v-for="(c, i) in topConvs" :key="c.conversation_id" class="top-row">
              <span class="rank">{{ i + 1 }}</span>
              <span class="top-title" :title="c.title">{{ c.title }}</span>
              <span class="top-meta">
                {{ c.calls }} 次 · {{ fmtTokens(c.total_tokens) }} tokens
              </span>
              <span class="top-cost">{{ fmtMoney(c.cost) }}</span>
            </div>
          </div>
        </NCard>

        <!-- 单价表 -->
        <NCard size="small" class="card">
          <template #header>
            <div class="card-head">
              <span>当前单价</span>
              <span class="hint">
                {{ pricingState.timezone }} · 高峰
                {{ (pricingState.peak_windows || []).join(' / ') }}
                <NTooltip trigger="hover">
                  <template #trigger><span class="src">来源</span></template>
                  <div class="src-tip">
                    <div>内置默认值：DeepSeek 官方价</div>
                    <div>配置文件：{{ pricingState.file }}</div>
                    <div>已加载：{{ pricingState.file_loaded ? '是' : '否（用内置值）' }}</div>
                    <div>环境变量：{{ pricingState.env_loaded ? '已覆盖' : '未设置' }}</div>
                  </div>
                </NTooltip>
              </span>
            </div>
          </template>
          <table class="price-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>厂商</th>
                <th v-if="hasCacheColumn">输入·缓存命中</th>
                <th>输入</th>
                <th>输出</th>
                <th>高峰</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in pricing" :key="p.model">
                <td>
                  {{ p.label }}
                  <span class="mono">{{ p.model }}</span>
                </td>
                <td>{{ p.vendor || '—' }}</td>
                <td v-if="hasCacheColumn">
                  {{ p.has_cache_price ? `${p.idle.in_cached} / ${p.peak.in_cached}` : '—' }}
                </td>
                <td>{{ p.idle.in }} / {{ p.peak.in }}</td>
                <td>{{ p.idle.out }} / {{ p.peak.out }}</td>
                <td>
                  <NTag v-if="hasPeak(p)" size="small" :bordered="false" type="warning">
                    ×{{ peakMul(p) }}
                  </NTag>
                  <span v-else class="muted">全天同价</span>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="unit">单位：元 / 百万 tokens；每格为「空闲 / 高峰」</p>
        </NCard>

        <!-- 明细 -->
        <NCard size="small" class="card">
          <template #header>调用明细</template>
          <NDataTable
            :columns="columns"
            :data="records"
            :bordered="false"
            :single-line="false"
            size="small"
            :row-key="(r: UsageRecord) => r.id"
          />
          <div class="pager">
            <NButton size="tiny" quaternary :disabled="page <= 0" @click="page = page - 1">
              上一页
            </NButton>
            <span class="pager-info">
              第 {{ page + 1 }} / {{ pageCount }} 页 · 共 {{ recordsTotal }} 条
            </span>
            <NButton
              size="tiny"
              quaternary
              :disabled="page + 1 >= pageCount"
              @click="page = page + 1"
            >
              下一页
            </NButton>
          </div>
        </NCard>

        <p class="disclaimer">
          费用按官方单价估算，<b>不含法定节假日日历</b>（节假日全天应算空闲，这里会按工作日算成高峰），
          也不含套餐与折扣。别拿这个页面对账，用它看趋势、找优化点。
        </p>
      </template>

      <NEmpty v-else-if="!loading" description="这段时间还没有 AI 调用记录" class="empty">
        <template #extra>
          <IconTextBtn name="calendar" size="small" @click="days = 0">看看全部</IconTextBtn>
        </template>
      </NEmpty>
    </NSpin>
  </div>
</template>

<style scoped>
.usage {
  max-width: 1080px;
  margin: 0 auto;
  padding: 4px 0 40px;
}
.head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.head h2 {
  margin: 0;
  font-size: 20px;
}
.sub {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--app-text-3);
}
.head-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.range {
  width: 120px;
}
.err,
.warn {
  margin-bottom: 14px;
}

.kpis {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}
.kpi {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kpi.accent {
  border-color: var(--app-primary-border-soft);
  background: linear-gradient(180deg, var(--app-surface-3) 0%, var(--app-surface) 100%);
}
.k-label {
  font-size: 12px;
  color: var(--app-text-3);
}
.k-value {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.4px;
}
.kpi.accent .k-value {
  color: var(--app-primary);
}
.k-foot {
  font-size: 12px;
  color: var(--app-text-3);
}
/* 免费那部分：钱是 0，但额度在消耗，得和「不花钱」区分开 */
.k-foot.free-note {
  display: block;
  margin-top: 2px;
  color: var(--app-teal);
}

.card {
  margin-bottom: 14px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.hint {
  font-size: 12px;
  color: var(--app-text-3);
  font-weight: 400;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.src {
  color: var(--app-primary);
  cursor: help;
  text-decoration: underline dotted;
}
.src-tip {
  font-size: 12px;
  line-height: 1.7;
}

.chart {
  width: 100%;
  height: auto;
  display: block;
  overflow: visible;
}
.chart.cost {
  margin-top: 2px;
}
.grid line,
.grid {
  stroke: var(--app-border);
  stroke-width: 1;
}
.axis-y text,
.axis-y {
  fill: var(--app-text-3);
  font-size: 11px;
}
.axis-x text {
  fill: var(--app-text-3);
  font-size: 10px;
}
.bar-label {
  fill: var(--app-text-2);
  font-size: 12px;
}
.bar-value {
  fill: var(--app-text);
  font-size: 12px;
}
.bar-bg {
  fill: var(--app-surface-2);
}
.cost-title {
  font-size: 12px;
  color: var(--app-text-3);
  margin-top: 10px;
}
.legend {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--app-text-3);
  font-weight: 400;
}
.legend i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  display: inline-block;
}
.legend i:not(:first-child) {
  margin-left: 8px;
}

.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.top-row {
  display: grid;
  grid-template-columns: 24px 1fr auto auto;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--app-surface-3);
  font-size: 13px;
}
.top-row:last-child {
  border-bottom: 0;
}
.rank {
  color: var(--app-text-3);
  font-variant-numeric: tabular-nums;
}
.top-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.top-meta {
  color: var(--app-text-3);
  font-size: 12px;
}
.top-cost {
  font-variant-numeric: tabular-nums;
  min-width: 90px;
  text-align: right;
}

.price-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.price-table th {
  text-align: left;
  font-weight: 500;
  color: var(--app-text-3);
  font-size: 12px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--app-border);
}
.price-table td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--app-surface-3);
  font-variant-numeric: tabular-nums;
}
.mono {
  display: block;
  font-size: 11px;
  color: var(--app-text-3);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.muted {
  color: var(--app-text-3);
  font-size: 12px;
}
.unit {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--app-text-3);
}

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 10px;
}
.pager-info {
  font-size: 12px;
  color: var(--app-text-3);
}
.disclaimer {
  margin: 16px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-text-3);
}
.empty {
  padding: 60px 0;
}

@media (max-width: 860px) {
  .kpis {
    grid-template-columns: repeat(2, 1fr);
  }
  .grid2 {
    grid-template-columns: 1fr;
  }
}
</style>
