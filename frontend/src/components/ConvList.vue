<script setup lang="ts">
/**
 * 会话列表（桌面侧栏 / 移动抽屉共用）。
 *
 * 只负责「展示 + 交互事件」，不直接碰 store —— 重命名/删除等动作由父组件执行，
 * 这样桌面（常驻侧栏）和移动（抽屉）能复用同一份 UI，行为一致。
 */
import { ref } from 'vue'
import { NButton, NEmpty, NInput, NPopconfirm, NScrollbar } from 'naive-ui'

import type { Conversation } from '@/types'
import IconTextBtn from '@/components/IconTextBtn.vue'
import ActionBtn from '@/components/ActionBtn.vue'
import IconSvg from '@/components/IconSvg.vue'

const props = withDefaults(
  defineProps<{
    conversations: Conversation[]
    currentId: string | null
    /** 抽屉里自带标题栏，列表不必再画一遍「会话」+「新建」，避免双重标题。 */
    showHeader?: boolean
  }>(),
  { showHeader: true },
)
void props

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'new'): void
  (e: 'rename', id: string, title: string): void
  (e: 'remove', id: string): void
}>()

// 编辑态：原地改名，回车/失焦提交，Esc 取消
const editingId = ref<string | null>(null)
const editingTitle = ref('')

function startRename(c: Conversation) {
  editingId.value = c.id
  editingTitle.value = c.title
}

function cancelRename() {
  editingId.value = null
}

/** 失焦或回车即提交；标题没变就跳过（由父组件做去重判断）。 */
function saveRename(c: Conversation) {
  const id = editingId.value
  if (!id) return
  editingId.value = null
  const next = editingTitle.value.trim()
  // 未见变化（含空白 -> 后端回落「新对话」的情况）就不打扰后端
  if (c.title === (next || '新对话')) return
  emit('rename', id, next)
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function onItemClick(c: Conversation) {
  if (editingId.value === c.id) return
  emit('select', c.id)
}
</script>

<template>
  <div class="conv-list">
    <div v-if="showHeader" class="conv-header">
      <span>会话</span>
      <IconTextBtn name="plus" size="tiny" type="primary" @click="emit('new')">新建</IconTextBtn>
    </div>
    <NScrollbar class="conv-scroll">
      <NEmpty v-if="!conversations.length" size="small" description="还没有会话" />
      <div
        v-for="c in conversations"
        :key="c.id"
        class="conv-item"
        :class="{ active: c.id === currentId }"
        @click="onItemClick(c)"
      >
        <NInput
          v-if="editingId === c.id"
          v-model:value="editingTitle"
          size="tiny"
          autofocus
          @click.stop
          @keydown.enter="saveRename(c)"
          @keydown.esc="cancelRename"
          @blur="saveRename(c)"
        />
        <template v-else>
          <div class="conv-main">
            <div class="conv-title">{{ c.title }}</div>
            <div class="conv-time">{{ fmtTime(c.updated_at) }}</div>
          </div>
          <div class="conv-actions">
            <ActionBtn name="edit" tip="重命名" @click.stop="startRename(c)" />
            <NPopconfirm @positive-click="emit('remove', c.id)">
              <template #trigger>
                <NButton quaternary circle size="tiny" type="error" title="删除会话">
                  <template #icon><IconSvg name="trash" /></template>
                </NButton>
              </template>
              删除后该会话的消息会一并清除，确定吗？
            </NPopconfirm>
          </div>
        </template>
      </div>
    </NScrollbar>
  </div>
</template>

<style scoped>
.conv-list {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.conv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  flex-shrink: 0;
}

/* 高度撑满容器（桌面侧栏 / 移动抽屉都靠父级给高度） */
.conv-scroll {
  flex: 1;
  min-height: 0;
}

.conv-item {
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: background 0.15s;
  display: flex;
  align-items: center;
  gap: 6px;
}

.conv-item:hover {
  background: var(--app-surface-2);
}

.conv-item.active {
  background: var(--app-primary-a14);
}

/* 标题区吃掉剩余宽度，min-width:0 是让 ellipsis 在 flex 里生效的关键 */
.conv-main {
  flex: 1;
  min-width: 0;
}

/* 操作按钮默认隐藏，鼠标悬停 / 选中时露出；移动端无 hover，靠 active 展示 */
.conv-actions {
  display: none;
  gap: 2px;
  flex-shrink: 0;
}

.conv-item:hover .conv-actions,
.conv-item.active .conv-actions {
  display: flex;
}

/* 触屏没有 hover：始终露出操作按钮，否则手机上无法重命名/删除 */
@media (hover: none) {
  .conv-actions {
    display: flex;
  }
}

.conv-title {
  font-size: 12.5px;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-time {
  font-size: 10.5px;
  color: var(--app-text-3);
  margin-top: 2px;
}
</style>
