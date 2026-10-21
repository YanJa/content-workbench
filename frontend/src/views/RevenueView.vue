<script setup lang="ts">
/**
 * 收益看板：流量主广告收益 / 曝光 / 点击 的可视化。
 *
 * 数据来自后端 /api/revenue/*（微信 publisher/stat，按 owner 隔离）。
 * 图表全部手绘 SVG（不引图表库，离线可用、包体更小）。
 *
 * 移动端：顶部筛选栏自动换行、KPI 卡片 2 列、图表铺满宽度。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NDatePicker,
  NEmpty,
  NGrid,
  NGridItem,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpin,
  NText,
} from 'naive-ui'

import {
  revenueAccounts,
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

async function load() {
  loading.value = true
  error.value = ''
  try {
    const aid = accountId.value === '__all__' ? null : accountId.value
    const [ov, st] = await Promise.all([
      revenueOverview({ account_id: aid, ...range.value }),
      revenueSettlement({ account_id: aid, ...range.value }),
    ])
    overview.value = ov
    settlement.value = st
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
const settledYuan = computed(() =>
  settlement.value ? yuan(settlement.value.totals.settled_revenue_all_fen) : '0.00',
)

// ---------------------------------------------------------------- 趋势图（SVG 手绘）
type Metric = 'income' | 'exposure' | 'click'
const metric = ref<Metric>('income')
const metricKey: Record<Metric, keyof TrendItem> = {
  income: 'income_fen',
  exposure: 'exposure',
  click: 'click',
}

const CHART_W = 720
const CHART_H = 220
const PAD = { top: 16, right: 16, bottom: 28, left: 48 }

const lineData = computed(() => {
  const trend = overview.value?.trend ?? []
  if (!trend.length) return null
  const key = metricKey[metric.value]
  const vals = trend.map((t) => Number(t[key]))
  const max = Math.max(1, ...vals)
  const innerW = CHART_W - PAD.left - PAD.right
  const innerH = CHART_H - PAD.top - PAD.bottom
  const n = trend.length
  const x = (i: number) => (n === 1 ? PAD.left + innerW / 2 : PAD.left + (innerW * i) / (n - 1))
  const y = (v: number) => PAD.top + innerH * (1 - v / max)
  const pts = trend.map((t, i) => [x(i), y(Number(t[key]))] as [number, number])
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ')
  const area =
    `M${pts[0][0].toFixed(1)} ${(PAD.top + innerH).toFixed(1)} ` +
    pts.map((p) => `L${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ') +
    ` L${pts[pts.length - 1][0].toFixed(1)} ${(PAD.top + innerH).toFixed(1)} Z`
  // x 轴标签：首 / 中 / 尾
  const labelIdx = n <= 2 ? [0] : [0, Math.floor((n - 1) / 2), n - 1]
  const labels = labelIdx.map((i) => ({ x: x(i), text: trend[i].date.slice(5) }))
  const maxY = metric.value === 'income' ? max / 100 : max
  return { line, area, labels, maxY: Math.ceil(maxY), baseY: PAD.top + innerH }
})

// ---------------------------------------------------------------- 收益构成
const composition = computed<CompositionItem[]>(() => overview.value?.composition ?? [])
const totalIncome = computed(() =>
  composition.value.reduce((s, c) => s + c.income_fen, 0),
)
function slotShare(fen: number): number {
  if (!totalIncome.value) return 0
  return (fen / totalIncome.value) * 100
}
const hasData = computed(() => (overview.value?.summary?.rows ?? 0) > 0)
</script>

<template>
  <div class="revenue-page">
    <!-- 筛选栏 -->
    <div class="filters">
      <NSelect
        v-model:value="accountId"
        :options="
          [{ label: '全部账号', value: '__all__' }].concat(
            accounts.map((a) => ({ label: a.name, value: a.id })),
          )
        "
        :consistent-menu-width="false"
        class="f-account"
        placeholder="选择公众号"
      />
      <NRadioGroup v-model:value="preset" size="small" class="f-preset" :on-update:value="onPresetChange">
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
      <NButton size="small" :loading="loading" @click="load">刷新</NButton>
    </div>

    <NText depth="3" class="range-hint">
      {{ range.start }} ~ {{ range.end }} · 共
      {{ overview?.account_count ?? 0 }} 个账号 · 数据 T+1 更新（当天未出）
    </NText>

    <NSpin :show="loading">
      <div v-if="error" class="error-box">{{ error }}</div>

      <template v-else-if="hasData">
        <!-- KPI 卡片 -->
        <NGrid cols="2 640:6" :x-gap="12" :y-gap="12" responsive="screen">
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">统计收益</div>
              <div class="kpi-value">¥{{ yuan(summary!.income_fen) }}</div>
            </NCard>
          </NGridItem>
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">已结算</div>
              <div class="kpi-value">¥{{ settledYuan }}</div>
            </NCard>
          </NGridItem>
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">广告曝光量</div>
              <div class="kpi-value">{{ int(summary!.exposure) }}</div>
            </NCard>
          </NGridItem>
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">广告点击量</div>
              <div class="kpi-value">{{ int(summary!.click) }}</div>
            </NCard>
          </NGridItem>
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">点击率</div>
              <div class="kpi-value">{{ pct(summary!.click_rate) }}</div>
            </NCard>
          </NGridItem>
          <NGridItem>
            <NCard size="small" class="kpi">
              <div class="kpi-label">千次曝光收益</div>
              <div class="kpi-value">¥{{ yuan(summary!.ecpm_fen) }}</div>
            </NCard>
          </NGridItem>
        </NGrid>

        <NGrid cols="1 880:2" :x-gap="16" :y-gap="16" responsive="screen" class="charts">
          <!-- 收益趋势 -->
          <NGridItem>
            <NCard size="small" title="收益趋势">
              <template #header-extra>
                <NRadioGroup v-model:value="metric" size="small">
                  <NRadioButton value="income">收益</NRadioButton>
                  <NRadioButton value="exposure">曝光</NRadioButton>
                  <NRadioButton value="click">点击</NRadioButton>
                </NRadioGroup>
              </template>
              <div v-if="lineData" class="chart-wrap">
                <svg :viewBox="`0 0 ${CHART_W} ${CHART_H}`" class="line-chart" preserveAspectRatio="none">
                  <defs>
                    <linearGradient id="revArea" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stop-color="var(--app-primary)" stop-opacity="0.28" />
                      <stop offset="100%" stop-color="var(--app-primary)" stop-opacity="0" />
                    </linearGradient>
                  </defs>
                  <line :x1="PAD.left" :y1="lineData.baseY" :x2="CHART_W - PAD.right" :y2="lineData.baseY" class="axis" />
                  <path :d="lineData.area" fill="url(#revArea)" />
                  <path :d="lineData.line" fill="none" class="line" />
                  <text
                    v-for="(lb, i) in lineData.labels"
                    :key="i"
                    :x="lb.x"
                    :y="CHART_H - 8"
                    class="axis-label"
                    text-anchor="middle"
                  >
                    {{ lb.text }}
                  </text>
                  <text :x="PAD.left" :y="PAD.top + 4" class="axis-max">{{ lineData.maxY }}</text>
                </svg>
              </div>
              <NEmpty v-else description="该区间暂无数据" size="small" />
            </NCard>
          </NGridItem>

          <!-- 收益构成 -->
          <NGridItem>
            <NCard size="small" title="收益构成（按广告位）">
              <div v-if="composition.length" class="composition">
                <div v-for="c in composition" :key="c.slot" class="comp-row">
                  <div class="comp-head">
                    <span class="comp-slot">{{ c.slot }}</span>
                    <span class="comp-val">¥{{ yuan(c.income_fen) }} · {{ slotShare(c.income_fen).toFixed(1) }}%</span>
                  </div>
                  <div class="comp-bar">
                    <div class="comp-fill" :style="{ width: slotShare(c.income_fen) + '%' }" />
                  </div>
                  <div class="comp-sub">曝光 {{ int(c.exposure) }} · 点击 {{ int(c.click) }}</div>
                </div>
              </div>
              <NEmpty v-else description="暂无收益构成数据" size="small" />
            </NCard>
          </NGridItem>
        </NGrid>
      </template>

      <NEmpty v-else description="该区间暂无广告数据（新号或尚未产生曝光）" />
    </NSpin>
  </div>
</template>

<style scoped>
.revenue-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.f-account {
  width: 200px;
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

.kpi {
  text-align: left;
}
.kpi-label {
  font-size: 12px;
  color: var(--app-text-3);
  margin-bottom: 6px;
}
.kpi-value {
  font-size: 22px;
  font-weight: 600;
  color: var(--app-text);
  line-height: 1.1;
}

.charts {
  margin-top: 4px;
}

.chart-wrap {
  width: 100%;
}
.line-chart {
  width: 100%;
  height: 220px;
  display: block;
}
.line {
  stroke: var(--app-primary);
  stroke-width: 2;
  vector-effect: non-scaling-stroke;
}
.axis {
  stroke: var(--app-border);
  stroke-width: 1;
}
.axis-label {
  fill: var(--app-text-3);
  font-size: 11px;
}
.axis-max {
  fill: var(--app-text-3);
  font-size: 11px;
}

.composition {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 4px;
}
.comp-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 13px;
}
.comp-slot {
  color: var(--app-text);
  font-weight: 500;
}
.comp-val {
  color: var(--app-text-2);
  font-size: 12px;
}
.comp-bar {
  height: 8px;
  border-radius: 4px;
  background: var(--app-border);
  overflow: hidden;
  margin: 4px 0 2px;
}
.comp-fill {
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--app-primary), var(--app-purple));
  transition: width 0.3s;
}
.comp-sub {
  font-size: 11px;
  color: var(--app-text-3);
}

@media (max-width: 640px) {
  .f-account {
    width: 100%;
  }
  .f-date {
    width: 100%;
  }
  .kpi-value {
    font-size: 18px;
  }
}
</style>
