<script setup lang="ts">
/**
 * 知乎热榜灵感卡：空白会话自动出现，也可从对话页工具栏「灵感」抽屉呼出。
 *
 * 三个设计决定，改之前先想清楚：
 *
 * 1. **数据全部来自本地库**（后端每半小时抓一次），浏览零 LLM 成本、零知乎
 *    额度消耗。组件本身不发任何请求，只渲染 props —— 数据由 ChatView 统一
 *    拉取，空白态和抽屉看到的永远是同一批。
 * 2. **「以此选题」只填输入框，不自动发送**。热点是给用户「不知道写什么」
 *    时用的起点，不是替他决定命题；填进去用户还能改，自动发就改不了了。
 * 3. **刷新按钮只给管理员**（`POST /refresh` 真实消耗共享额度池，普通人
 *    点了也是 403）。普通用户看到的热点有最多半小时延迟，这是故意的 ——
 *    灵感不需要秒级新鲜。
 */
import { computed } from 'vue'
import { NButton, NSpin, NTag } from 'naive-ui'

import { useAuthStore } from '@/stores/auth'
import IconTextBtn from './IconTextBtn.vue'
import type { HotTopicItem } from '@/api/zhihu'

const props = defineProps<{
  items: HotTopicItem[]
  loading: boolean
  error: string
  /** 数据批次时间（unix 秒），0 = 未知 */
  fetchedAt?: number
  /** 后端显式返回的凭证配置状态；undefined = 已配置 */
  configured?: boolean
}>()

const emit = defineEmits<{
  (e: 'use', topic: HotTopicItem): void
  (e: 'refresh'): void
}>()

const auth = useAuthStore()

/** 批次时间：今天只显示时分，跨天带上日期（热点是当日话题，不需要年份） */
const fetchedLabel = computed(() => {
  if (!props.fetchedAt) return ''
  const d = new Date(props.fetchedAt * 1000)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay ? `今天 ${hm}` : `${d.getMonth() + 1}/${d.getDate()} ${hm}`
})

/** 没配凭证且没有历史数据：对普通用户也没有存在意义，给一句弱提示就够 */
const unavailable = computed(() => props.configured === false && !props.items.length)

/** 在榜时长取整显示；后端给的是小数小时（如 3.5） */
function stayingLabel(hours: number): string {
  return `在榜 ${Math.round(hours)} 小时`
}
</script>

<template>
  <section class="hot-inspire">
    <header class="hot-head">
      <span class="hot-title">🔥 知乎热榜灵感</span>
      <span v-if="fetchedLabel" class="hot-time">更新于 {{ fetchedLabel }}</span>
      <div class="hot-head-spacer" />
      <IconTextBtn
        v-if="auth.isAdmin"
        name="reset"
        size="tiny"
        quaternary
        :loading="loading"
        @click="emit('refresh')"
      >
        刷新
      </IconTextBtn>
    </header>

    <div v-if="loading && !items.length" class="hot-state">
      <NSpin :size="14" />
      <span>正在加载热点…</span>
    </div>
    <div v-else-if="unavailable" class="hot-state">热点灵感未配置，联系管理员开启</div>
    <div v-else-if="error && !items.length" class="hot-state">
      <span>热点加载失败</span>
      <NButton text type="primary" size="tiny" @click="emit('refresh')">重试</NButton>
    </div>
    <div v-else-if="!items.length" class="hot-state">热点准备中，每 30 分钟自动更新</div>

    <ul v-else class="hot-list">
      <li v-for="t in items" :key="t.question_id" class="hot-row">
        <span class="hot-rank" :class="{ 'hot-rank-top': t.rank <= 3 }">{{ t.rank }}</span>
        <div class="hot-body">
          <div class="hot-row-title">
            <a class="hot-link" :href="t.url" target="_blank" rel="noopener noreferrer">{{
              t.title
            }}</a>
          </div>
          <div class="hot-tags">
            <NTag v-if="t.climb > 0" size="tiny" :bordered="false" type="success">
              ↑ {{ t.climb }} 位
            </NTag>
            <NTag v-if="t.is_new" size="tiny" :bordered="false" type="warning">🆕 新上榜</NTag>
            <NTag v-if="t.staying_hours >= 2" size="tiny" :bordered="false">
              {{ stayingLabel(t.staying_hours) }}
            </NTag>
          </div>
          <p v-if="t.summary" class="hot-summary">{{ t.summary }}</p>
        </div>
        <NButton class="hot-use" size="tiny" secondary type="primary" @click="emit('use', t)">
          以此选题
        </NButton>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.hot-inspire {
  text-align: left;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-surface-2);
  overflow: hidden;
}

.hot-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--app-border-50);
}

.hot-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--app-text-2);
}

.hot-time {
  font-size: 11px;
  color: var(--app-text-3);
}

.hot-head-spacer {
  flex: 1;
}

.hot-state {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 12px;
  font-size: 12px;
  color: var(--app-text-3);
}

.hot-list {
  margin: 0;
  padding: 4px 0;
  list-style: none;
}

.hot-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 12px;
}

.hot-row + .hot-row {
  border-top: 1px solid var(--app-border-50);
}

.hot-row:hover {
  background: var(--app-surface-4);
}

/* 名次徽章：前三名用主色强调，其余弱化 —— 一眼看出「哪条最值得写」 */
.hot-rank {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  background: var(--app-surface-4);
  color: var(--app-text-3);
  font-variant-numeric: tabular-nums;
}

.hot-rank-top {
  background: var(--app-primary-a18);
  color: var(--app-primary-soft);
}

.hot-body {
  flex: 1;
  min-width: 0;
}

.hot-row-title {
  font-size: 13px;
  line-height: 1.5;
}

.hot-link {
  color: var(--app-text);
  text-decoration: none;
}

.hot-link:hover {
  color: var(--app-primary-soft);
  text-decoration: underline;
}

.hot-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 3px;
}

/* 摘要两行截断：平台白送的 ~200 字，全展开会淹没榜单 */
.hot-summary {
  margin: 4px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--app-text-3);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.hot-use {
  flex-shrink: 0;
  margin-top: 1px;
}
</style>
