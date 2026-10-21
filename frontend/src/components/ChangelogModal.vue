<script setup lang="ts">
/**
 * 更新日志弹窗：复用同一套 UI 服务于两个场景——
 * - 自动「新功能」提醒：只传比用户已看版本更新的 entries；
 * - 手动「更新日志」入口：传全部历史 entries。
 *
 * 纯展示，不依赖任何业务状态；版本比较与「已看」推进在 version store 里做。
 */
import { NButton, NModal } from 'naive-ui'

import type { ChangelogEntry } from '@/version/changelog'

// 仅模板使用这些 props（entries / title / show），无需在脚本里取用
defineProps<{
  show: boolean
  entries: ChangelogEntry[]
  /** 弹窗标题，如「新功能」「更新日志」 */
  title?: string
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'close'): void
}>()

function close(): void {
  emit('update:show', false)
  emit('close')
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="title || '更新日志'"
    style="max-width: 520px; width: calc(100vw - 32px)"
    :auto-focus="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="clog">
      <section v-for="e in entries" :key="e.version" class="clog-entry">
        <header class="clog-head">
          <span class="clog-ver">v{{ e.version }}</span>
          <span class="clog-date">{{ e.date }}</span>
        </header>
        <div v-if="e.title" class="clog-title">{{ e.title }}</div>
        <ul class="clog-list">
          <li v-for="(h, i) in e.highlights" :key="i">{{ h }}</li>
        </ul>
      </section>
    </div>

    <template #footer>
      <div class="clog-foot">
        <NButton block type="primary" @click="close">我知道了</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.clog {
  max-height: 56vh;
  overflow-y: auto;
}

.clog-entry + .clog-entry {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px solid var(--app-border);
}

/* 第一行：版本号 + 日期（左右两端），均不收缩不换行 */
.clog-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 6px;
}

.clog-ver {
  flex-shrink: 0;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 700;
  color: var(--app-text);
}

.clog-date {
  margin-left: auto;
  flex-shrink: 0;
  white-space: nowrap;
  font-size: 12px;
  color: var(--app-text-3);
  font-variant-numeric: tabular-nums;
}

/* 标题 chip：另起一行，长了就在 chip 内部换行（NTag 自带 nowrap 用不了） */
.clog-title {
  display: inline-block;
  max-width: 100%;
  margin-bottom: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.6;
  color: var(--app-primary-soft);
  background: var(--app-primary-a14);
}

.clog-list {
  margin: 0;
  padding-left: 18px;
}

.clog-list li {
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-2);
  margin-bottom: 4px;
}

.clog-foot {
  display: flex;
}
</style>
