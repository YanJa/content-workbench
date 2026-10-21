<script setup lang="ts">
/**
 * 收益看板：流量主广告的 收益 / 曝光 / 点击 可视化，并展示「结算」进度。
 *
 * 数据来自后端 /api/revenue/*（微信 publisher/stat，按 owner 隔离）。
 * 图表全部手绘 SVG（不引图表库，离线可用、包体更小），设计对标「AI 用量」页。
 *
 * 「已结算怎么展示」的口径（页面已如实标注，不用此页对账、只看量级与趋势）：
 *   - 统计收益  = 所选区间内每日明细合计（本期口径）；
 *   - 累计收入 / 已结算 / 扣罚 = 微信结算口径，**累计值**，与上方时间筛选无关；
 *   - 待结算    = 累计收入 − 已结算 − 扣罚（估算，即「还没打款」的那部分）。
 *   两者有 T+1 与月结时滞，且微信自身两口径可能差 1 分，故分别标注来源。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  NCard,
  NDatePicker,
  NDropdown,
  NEmpty,
  NGrid,
  NGridItem,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpin,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'

import IconTextBtn from '@/components/IconTextBtn.vue'
import {
  revenueAccounts,
  revenueExport,
  revenueOverview,
  revenueSettlement,
  type CompositionItem,
  type RevenueAccount,
  type RevenueOverview,
  type RevenueSettlement,
  type TrendItem,
} from '@/api/revenue'

// ---------------------------------------------------------------- 筛选状态
const accounts = ref<RevenueAccount[]>([])
const accountId = ref<string>('__all__') // '__all__' = 全部账号聚合
const preset = ref<'yesterday' | '7' | '30' | '90' | ''>('30')
const customRange = ref<[number, number] | null>(null) // 自定义：[startTs, endTs]

// 把日期格式化成 YYYY-MM-DD（本地时区，避免 toISOString 的 UTC 偏移）
function fmtDate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
function shiftDays(base: Date, delta: number): string {
  const d = new Date(base)
  d.setDate(d.getDate() + delta)
  return fmtDate(d)
}

const range = computed(() => {
  if (customRange.value) {
    return {
      start: fmtDate(new Date(customRange.value[0])),
      end: fmtDate(new Date(customRange.value[1])),
    }
  }
  const today = new Date()
  if (preset.value === 'yesterday') {
    const y = shiftDays(today, -1)
    return { start: y, end: y }
  }
  const map: Record<string, number> = { '7': -6, '30': -29, '90': -89 }
  return { start: shiftDays(today, map[preset.value]), end: fmtDate(today) }
})

/** 选了自定义区间时，清掉预设高亮（二者互斥，customRange 非空时以它为准）。 */
function onCustomPick() {
  preset.value = ''
}
/** 点预设时清空自定义区间。 */
function onPresetChange(v: 'yesterday' | '7' | '30' | '90' | '') {
  customRange.value = null
  preset.value = v
}

// ---------------------------------------------------------------- 数据
const loading = ref(false)
const error = ref('')
const overview = ref<RevenueOverview | null>(null)
const settlement = ref<RevenueSettlement | null>(null)
const msg = useMessage()

// ---------------------------------------------------------------- 导出
const exporting = ref(false)
const exportOptions = [
  { label: 'Excel 表格（.xlsx，含汇总/构成）', key: 'xlsx' },
  { label: 'CSV 明细（.csv，天×广告位）', key: 'csv' },
]

/** 按当前筛选条件导出；后端只导已缓存数据，区间为空时会抛出带文案的 404。 */
async function onExport(key: string) {
  if (key !== 'xlsx' && key !== 'csv') return
  exporting.value = true
  try {
    await revenueExport({
      account_id: accountId.value === '__all__' ? null : accountId.value,
      start: range.value.start,
      end: range.value.end,
      format: key,
    })
    msg.success('已开始下载')
  } catch (e: any) {
    msg.error(e?.message || '导出失败')
  } finally {
    exporting.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  const aid = accountId.value === '__all__' ? null : accountId.value
  try {
    // 结算接口每次都打微信（无缓存），失败不该拖垮整个看板 —— 用 allSettled 拆开：
    // overview 是主数据，失败即整体报错；settlement 尽力而为，失败只留空。
    const [ovR, stR] = await Promise.allSettled([
      revenueOverview({ account_id: aid, ...range.value }),
      revenueSettlement({ account_id: aid, ...range.value }),
    ])
    if (ovR.status === 'rejected') throw ovR.reason
    overview.value = ovR.value
    settlement.value = stR.status === 'fulfilled' ? stR.value : null
  } catch (e: any) {
    error.value = e?.message || '加载失败'
    overview.value = null
    settlement.value = null
  } finally {
    loading.value = false
  }
}

// 筛选变化即重载
watch([accountId, preset, customRange], load)

// 首次加载：先取账号列表，再拉数据
onMounted(async () => {
  try {
    accounts.value = await revenueAccounts()
  } catch {
    /* 没配公众号时后端会 404，下面 load 会提示 */
  }
  await load()
})

// ---------------------------------------------------------------- 格式化
function yuan(fen: number): string {
  return (fen / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function int(n: number): string {
  return n.toLocaleString('zh-CN')
}
function pct(ratio: number): string {
  return (ratio * 100).toFixed(2) + '%'
}

const summary = computed(() => overview.value?.summary ?? null)
const hasData = computed(() => (overview.value?.summary?.rows ?? 0) > 0)

// ---------------------------------------------------------------- 结算口径
const stTotals = computed(() => settlement.value?.totals ?? null)
const revenueAll = computed(() => stTotals.value?.revenue_all_fen ?? 0) // 累计收入
const settledAll = computed(() => stTotals.value?.settled_revenue_all_fen ?? 0) // 已结算
const penaltyAll = computed(() => stTotals.value?.penalty_all_fen ?? 0) // 扣罚
const pendingAll = computed(() => Math.max(0, revenueAll.value - settledAll.value - penaltyAll.value)) // 待结算
const settledRatio = computed(() => (revenueAll.value ? (settledAll.value / revenueAll.value) * 100 : 0))

interface SettleRow {
  month: string
  amount_fen: number
  sett_no: string
  account: string
}
/** 把所有账号的月度结算单摊平，按月份倒序（最新在上）。 */
const settleRows = computed<SettleRow[]>(() => {
  const out: SettleRow[] = []
  for (const it of settlement.value?.items ?? []) {
    for (const s of it.settlement_list ?? []) {
      out.push({
        month: s.month,
        amount_fen: Number(s.settled_revenue) || 0,
        sett_no: s.sett_no || '',
        account: it.name,
      })
    }
  }
  return out.sort((a, b) => b.month.localeCompare(a.month))
})
const multiAccount = computed(() => (overview.value?.account_count ?? 0) > 1)
function fmtMonth(m: string): string {
  if (!m || m.length < 6) return m || '—'
  return `${m.slice(0, 4)} 年 ${m.slice(4, 6)} 月`
}

// ---------------------------------------------------------------- 趋势图（SVG 手绘）
type Metric = 'income' | 'exposure' | 'click'
const metric = ref<Metric>('income')
const metricKey: Record<Metric, keyof TrendItem> = {
  income: 'income_fen',
  exposure: 'exposure',
  click: 'click',
}
const METRIC_LABEL: Record<Metric, string> = { income: '收益', exposure: '曝光', click: '点击' }
const METRIC_COLOR: Record<Metric, string> = {
  income: 'var(--app-primary)',
  exposure: 'var(--app-teal)',
  click: 'var(--app-amber)',
}
const metricColor = computed(() => METRIC_COLOR[metric.value])
const metricUnit = computed(() => (metric.value === 'income' ? '元' : '次'))

const CHART_W = 760
const CHART_H = 230
const PAD = { top: 14, right: 14, bottom: 28, left: 52 }

/** 把 Y 轴最大值抬到一个好看的整数，否则刻度会出现 8734 这种鬼数字 */
function niceMax(v: number): number {
  if (v <= 0) return 1
  const exp = Math.floor(Math.log10(v))
  const base = 10 ** exp
  const n = v / base
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * base
}
/** Y 轴刻度文字：收益按「元」显示，曝光/点击按原始次数（带万/k 缩略）。 */
function fmtAxis(v: number): string {
  const val = metric.value === 'income' ? v / 100 : v
  if (val >= 10000) return `${(val / 10000).toFixed(val >= 100000 ? 0 : 1)}万`
  if (val >= 1000) return `${Math.round(val / 1000)}k`
  return String(Math.round(val))
}

const trendChart = computed(() => {
  const trend = overview.value?.trend ?? []
  if (!trend.length) return null
  const key = metricKey[metric.value]
  const raw = trend.map((t) => Number(t[key]) || 0)
  const yMax = niceMax(Math.max(...raw, 1))
  const plotW = CHART_W - PAD.left - PAD.right
  const plotH = CHART_H - PAD.top - PAD.bottom
  const baseY = PAD.top + plotH
  const n = trend.length
  const slot = plotW / n
  const cx = (i: number) => PAD.left + slot * i + slot / 2
  const cy = (v: number) => baseY - (v / yMax) * plotH
  const pts = trend.map((t, i) => ({ x: cx(i), y: cy(Number(t[key]) || 0), d: t }))
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')
  const area =
    `M${PAD.left} ${baseY} ` +
    pts.map((p) => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ') +
    ` L${PAD.left + plotW} ${baseY} Z`
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => ({ value: yMax * t, y: baseY - plotH * t }))
  // X 轴标签：抽稀到 ~10 个，否则全糊在一起
  const step = Math.max(1, Math.ceil(n / 10))
  const xLabels = trend
    .map((t, i) => ({ text: t.date.slice(5), x: cx(i), i }))
    .filter(({ i }) => i % step === 0 || i === n - 1)
  return { W: CHART_W, H: CHART_H, PAD, plotW, baseY, yMax, ticks, pts, line, area, xLabels }
})

// ---------------------------------------------------------------- 收益构成
const composition = computed<CompositionItem[]>(() => overview.value?.composition ?? [])
const totalIncome = computed(() => composition.value.reduce((s, c) => s + c.income_fen, 0))
function slotShare(fen: number): number {
  if (!totalIncome.value) return 0
  return (fen / totalIncome.value) * 100
}
const SLOT_COLORS = ['var(--app-primary)', 'var(--app-purple)', 'var(--app-teal)', 'var(--app-amber)']
function slotColor(i: number): string {
  return SLOT_COLORS[i % SLOT_COLORS.length]
}
</script>

<template>
  <div class="revenue-page">
    <!-- 页头：标题 + 副标题 + 账号/刷新 -->
    <div class="head">
      <div>
        <h2>收益看板</h2>
        <p class="sub">流量主广告赚了多少、曝光点击如何、钱到账没有</p>
      </div>
      <div class="head-right">
        <NSelect
          v-model:value="accountId"
          :options="
            [{ label: '全部账号', value: '__all__' }].concat(
              accounts.map((a) => ({ label: a.name, value: a.id })),
            )
          "
          :consistent-menu-width="false"
          class="f-account"
          size="small"
          placeholder="选择公众号"
        />
        <NDropdown trigger="click" :options="exportOptions" @select="onExport">
          <IconTextBtn name="download" size="small" quaternary :loading="exporting">
            导出
          </IconTextBtn>
        </NDropdown>
        <IconTextBtn name="reset" size="small" quaternary :loading="loading" @click="load">
          刷新
        </IconTextBtn>
      </div>
    </div>

    <!-- 时间筛选 -->
    <div class="filters">
      <NRadioGroup
        v-model:value="preset"
        size="small"
        class="f-preset"
        :on-update:value="onPresetChange"
      >
        <NRadioButton value="yesterday">昨日</NRadioButton>
        <NRadioButton value="7">近7天</NRadioButton>
        <NRadioButton value="30">近30天</NRadioButton>
        <NRadioButton value="90">近90天</NRadioButton>
      </NRadioGroup>
      <NDatePicker
        v-model:value="customRange"
        type="daterange"
        size="small"
        class="f-date"
        :on-confirm="onCustomPick"
        clearable
        placeholder="自定义区间"
      />
      <NText depth="3" class="range-hint">
        {{ range.start }} ~ {{ range.end }} · 共 {{ overview?.account_count ?? 0 }} 个账号 · 数据 T+1（当天未出）
      </NText>
    </div>

    <NSpin :show="loading">
      <div v-if="error" class="error-box">{{ error }}</div>

      <template v-else-if="hasData">
        <!-- 资金 KPI：本期收益 + 结算三件套（累计口径，与时间筛选无关）
             注意：NGrid 的 responsive 默认 "self"（按栅格自身宽度断点），
             数字断点（640/880）才能生效；若写 "screen" 会按命名断点
             （xs/s/m/l…）匹配，数字键永远命中不了，网格会锁死在默认列数。 -->
        <NGrid cols="2 640:4" :x-gap="12" :y-gap="12" class="kpis">
          <NGridItem>
            <div class="kpi accent">
              <span class="k-label">统计收益</span>
              <strong class="k-value">¥{{ yuan(summary!.income_fen) }}</strong>
              <span class="k-foot">本期明细合计 · 曝光 {{ int(summary!.exposure) }}</span>
            </div>
          </NGridItem>
          <NGridItem>
            <div class="kpi">
              <span class="k-label">累计收入</span>
              <strong class="k-value">¥{{ yuan(revenueAll) }}</strong>
              <span class="k-foot">微信结算口径 · 含扣罚 ¥{{ yuan(penaltyAll) }}</span>
            </div>
          </NGridItem>
          <NGridItem>
            <div class="kpi">
              <span class="k-label">
                已结算
                <NTag v-if="settledAll > 0" size="tiny" :bordered="false" type="success">已打款</NTag>
              </span>
              <strong class="k-value settled">¥{{ yuan(settledAll) }}</strong>
              <span class="k-foot">累计收入的 {{ settledRatio.toFixed(1) }}%</span>
            </div>
          </NGridItem>
          <NGridItem>
            <div class="kpi">
              <span class="k-label">待结算</span>
              <strong class="k-value pending">¥{{ yuan(pendingAll) }}</strong>
              <span class="k-foot">累计 − 已结算 − 扣罚</span>
            </div>
          </NGridItem>
        </NGrid>

        <!-- 广告效果指标 -->
        <div class="metrics">
          <div class="metric">
            <span class="m-label">广告曝光量</span>
            <strong class="m-value">{{ int(summary!.exposure) }}</strong>
          </div>
          <div class="metric">
            <span class="m-label">广告点击量</span>
            <strong class="m-value">{{ int(summary!.click) }}</strong>
          </div>
          <div class="metric">
            <span class="m-label">点击率</span>
            <strong class="m-value">{{ pct(summary!.click_rate) }}</strong>
          </div>
          <div class="metric">
            <span class="m-label">千次曝光收益</span>
            <strong class="m-value">¥{{ yuan(summary!.ecpm_fen) }}</strong>
          </div>
        </div>

        <!-- 趋势 + 构成（≥880px 两列并排，窄屏堆叠） -->
        <NGrid cols="1 880:2" :x-gap="16" :y-gap="16" class="charts">
          <NGridItem>
            <NCard size="small" class="card">
              <template #header>
                <!-- 标题与「单位」合成一个左对齐组：header-extra 放指标切换，
                     单位若单独成 span 会被 space-between 推到切换按钮旁边，贴在一起 -->
                <span class="chart-title">
                  收益趋势<span class="hint">单位：{{ metricUnit }}</span>
                </span>
              </template>
              <template #header-extra>
                <NRadioGroup v-model:value="metric" size="small">
                  <NRadioButton value="income">收益</NRadioButton>
                  <NRadioButton value="exposure">曝光</NRadioButton>
                  <NRadioButton value="click">点击</NRadioButton>
                </NRadioGroup>
              </template>
              <div v-if="trendChart" class="chart-wrap">
                <svg
                  :viewBox="`0 0 ${trendChart.W} ${trendChart.H}`"
                  class="chart"
                  preserveAspectRatio="xMidYMid meet"
                  role="img"
                  aria-label="收益趋势图"
                >
                  <defs>
                    <linearGradient id="revFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" :style="{ stopColor: metricColor }" stop-opacity="0.28" />
                      <stop offset="100%" :style="{ stopColor: metricColor }" stop-opacity="0" />
                    </linearGradient>
                  </defs>
                  <g class="grid">
                    <line
                      v-for="t in trendChart.ticks"
                      :key="t.value"
                      :x1="trendChart.PAD.left"
                      :x2="trendChart.W - trendChart.PAD.right"
                      :y1="t.y"
                      :y2="t.y"
                    />
                  </g>
                  <g class="axis-y">
                    <text
                      v-for="t in trendChart.ticks"
                      :key="t.value"
                      :x="trendChart.PAD.left - 8"
                      :y="t.y + 4"
                      text-anchor="end"
                    >
                      {{ fmtAxis(t.value) }}
                    </text>
                  </g>
                  <path :d="trendChart.area" fill="url(#revFill)" />
                  <path
                    :d="trendChart.line"
                    fill="none"
                    :style="{ stroke: metricColor }"
                    stroke-width="2"
                    stroke-linejoin="round"
                    stroke-linecap="round"
                  />
                  <g v-for="(p, i) in trendChart.pts" :key="i">
                    <title>
                      {{ p.d.date }}｜{{ METRIC_LABEL[metric] }}
                      {{ metric === 'income' ? '¥' + yuan(p.d.income_fen) : int(Number(p.d[metricKey[metric]])) }}
                    </title>
                    <circle :cx="p.x" :cy="p.y" r="2.6" :style="{ fill: metricColor }" />
                  </g>
                  <g class="axis-x">
                    <text
                      v-for="(l, i) in trendChart.xLabels"
                      :key="i"
                      :x="l.x"
                      :y="trendChart.H - 8"
                      text-anchor="middle"
                    >
                      {{ l.text }}
                    </text>
                  </g>
                </svg>
              </div>
              <NEmpty v-else description="该区间暂无数据" size="small" />
            </NCard>
          </NGridItem>

          <NGridItem>
            <NCard size="small" title="收益构成（按广告位）" class="card">
              <div v-if="composition.length" class="composition">
                <div v-for="(c, i) in composition" :key="c.slot" class="comp-row">
                  <div class="comp-top">
                    <span class="comp-name">
                      <i class="comp-dot" :style="{ background: slotColor(i) }" />{{ c.slot }}
                    </span>
                    <span class="comp-amt">¥{{ yuan(c.income_fen) }}<em>{{ slotShare(c.income_fen).toFixed(1) }}%</em></span>
                  </div>
                  <div class="comp-bar">
                    <div
                      class="comp-fill"
                      :style="{ width: slotShare(c.income_fen) + '%', background: slotColor(i) }"
                    />
                  </div>
                  <div class="comp-sub">曝光 {{ int(c.exposure) }} · 点击 {{ int(c.click) }}</div>
                </div>
              </div>
              <NEmpty v-else description="暂无收益构成数据" size="small" />
            </NCard>
          </NGridItem>
        </NGrid>

        <!-- 结算进度：累计口径，回答「已结算/待结算」 -->
        <NCard size="small" class="card settle-card">
          <template #header>
            <div class="card-head">
              <span>结算进度</span>
              <span class="hint">累计口径 · 与上方时间筛选无关</span>
            </div>
          </template>

          <div class="settle-progress">
            <div class="sp-track">
              <div class="sp-fill" :style="{ width: Math.max(settledRatio, settledAll > 0 ? 1.5 : 0) + '%' }" />
            </div>
            <div class="sp-legend">
              <span class="sp-settled">
                <i style="background: var(--app-teal)" />已结算 {{ settledRatio.toFixed(1) }}（¥{{ yuan(settledAll) }}）
              </span>
              <span class="sp-pending">待结算 ¥{{ yuan(pendingAll) }}</span>
            </div>
          </div>

          <div class="settle-break">
            <div class="sb-item">
              <span class="sb-label">已结算</span>
              <strong class="sb-value settled">¥{{ yuan(settledAll) }}</strong>
            </div>
            <div class="sb-item">
              <span class="sb-label">待结算</span>
              <strong class="sb-value pending">¥{{ yuan(pendingAll) }}</strong>
            </div>
            <div class="sb-item">
              <span class="sb-label">扣罚</span>
              <strong class="sb-value">¥{{ yuan(penaltyAll) }}</strong>
            </div>
          </div>

          <div v-if="settleRows.length" class="settle-table-wrap">
            <table class="settle-table">
              <thead>
                <tr>
                  <th>结算月份</th>
                  <th v-if="multiAccount">账号</th>
                  <th class="num">已结算金额</th>
                  <th>结算单号</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in settleRows" :key="i">
                  <td>{{ fmtMonth(r.month) }}</td>
                  <td v-if="multiAccount">{{ r.account }}</td>
                  <td class="num">¥{{ yuan(r.amount_fen) }}</td>
                  <td class="mono">{{ r.sett_no || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <NEmpty v-else description="暂无结算单（尚未到结算周期）" size="small" class="settle-empty" />

          <p class="settle-note">
            结算是**累计口径、按月出账**：本月产生的收益通常次月中下旬才打款，所以「待结算」会一直有余额，属正常。
          </p>
        </NCard>

        <p class="disclaimer">
          数据来自微信广告分析接口，<b>T+1 更新</b>，仅统计广告收益（不含赞赏 / 带货 / 付费阅读）。
          「统计收益」为区间明细合计，「累计收入」为微信结算口径，两者因月结时滞与微信口径可能有 1 分以内差异——
          本页用于看趋势与量级，<b>不作为对账依据</b>。
        </p>
      </template>

      <NEmpty v-else description="该区间暂无广告数据（新号或尚未产生曝光）" class="empty" />
    </NSpin>
  </div>
</template>

<style scoped>
.revenue-page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 4px 0 40px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* NSpin 的内容容器（.n-spin-content）是 block，页容器的 flex gap 传不进去，
   会导致 KPI 组 / 指标条 / 图表组 / 结算卡之间 0 间距挨在一起。
   在这里给容器补上 flex + gap，各组卡片恢复 14px 间距。 */
.revenue-page :deep(.n-spin-content) {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
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
.f-account {
  width: 160px;
}

/* 时间筛选行 */
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.f-preset {
  flex-wrap: wrap;
}
.f-date {
  width: 240px;
}
.range-hint {
  font-size: 12px;
}

.error-box {
  padding: 16px;
  border-radius: 8px;
  background: rgba(245, 34, 45, 0.08);
  color: #f5222d;
  font-size: 13px;
}

/* 资金 KPI 卡 */
.kpis {
  margin: 0;
}
.kpi {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  height: 100%;
}
.kpi.accent {
  border-color: var(--app-primary-border-soft);
  background: linear-gradient(180deg, var(--app-surface-3) 0%, var(--app-surface) 100%);
}
.k-label {
  font-size: 12px;
  color: var(--app-text-3);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.k-value {
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.4px;
  line-height: 1.1;
}
.kpi.accent .k-value {
  color: var(--app-primary);
}
.k-value.settled {
  color: var(--app-teal);
}
.k-value.pending {
  color: var(--app-amber);
}
.k-foot {
  font-size: 12px;
  color: var(--app-text-3);
}

/* 广告效果指标条 */
.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 12px 16px;
}
.metric {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.metric + .metric {
  border-left: 1px solid var(--app-border);
  padding-left: 16px;
}
.m-label {
  font-size: 12px;
  color: var(--app-text-3);
}
.m-value {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* 图表卡 */
.card {
  height: 100%;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
/* 趋势卡标题组：标题 + 单位说明，左对齐、带间隔 */
.chart-title {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-weight: 500;
}
.hint {
  font-size: 12px;
  color: var(--app-text-3);
  font-weight: 400;
}
.chart-wrap {
  width: 100%;
}
.chart {
  width: 100%;
  height: auto;
  display: block;
  overflow: visible;
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

/* 收益构成 */
.composition {
  display: flex;
  flex-direction: column;
  gap: 16px;
  margin-top: 6px;
}
.comp-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
}
.comp-name {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 500;
  color: var(--app-text);
}
.comp-dot {
  width: 9px;
  height: 9px;
  border-radius: 3px;
  display: inline-block;
  flex-shrink: 0;
}
.comp-amt {
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.comp-amt em {
  font-style: normal;
  font-weight: 400;
  color: var(--app-text-3);
  font-size: 12px;
  margin-left: 6px;
}
.comp-bar {
  height: 8px;
  border-radius: 4px;
  background: var(--app-surface-2);
  overflow: hidden;
  margin: 6px 0 4px;
}
.comp-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.3s;
}
.comp-sub {
  font-size: 11px;
  color: var(--app-text-3);
}

/* 结算进度卡 */
.settle-card :deep(.n-card__content) {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.settle-progress {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sp-track {
  height: 12px;
  border-radius: 6px;
  background: var(--app-surface-2);
  overflow: hidden;
}
.sp-fill {
  height: 100%;
  border-radius: 6px;
  background: linear-gradient(90deg, var(--app-teal), var(--app-primary));
  transition: width 0.4s;
}
.sp-legend {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--app-text-2);
}
.sp-settled {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.sp-settled i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  display: inline-block;
}
.sp-pending {
  color: var(--app-text-3);
}
.settle-break {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.sb-item {
  background: var(--app-surface-2);
  border-radius: 10px;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.sb-label {
  font-size: 12px;
  color: var(--app-text-3);
}
.sb-value {
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.sb-value.settled {
  color: var(--app-teal);
}
.sb-value.pending {
  color: var(--app-amber);
}
.settle-table-wrap {
  overflow-x: auto;
}
.settle-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.settle-table th {
  text-align: left;
  font-weight: 500;
  color: var(--app-text-3);
  font-size: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--app-border);
}
.settle-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--app-surface-3);
  font-variant-numeric: tabular-nums;
}
.settle-table tr:last-child td {
  border-bottom: 0;
}
.settle-table .num {
  text-align: right;
}
.settle-table .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--app-text-3);
}
.settle-empty {
  padding: 16px 0;
}
.settle-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-text-3);
}

.disclaimer {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-text-3);
}
.empty {
  padding: 60px 0;
}

@media (max-width: 640px) {
  .f-account {
    width: 100%;
  }
  .f-date {
    width: 100%;
  }
  .k-value {
    font-size: 20px;
  }
  .metrics {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px 12px;
  }
  .metric:nth-child(odd) + .metric {
    border-left: 0;
    padding-left: 0;
  }
  .metric + .metric {
    border-left: 0;
    padding-left: 0;
  }
  .settle-break {
    grid-template-columns: 1fr;
  }
}
</style>
