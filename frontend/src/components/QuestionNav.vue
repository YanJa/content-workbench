<script setup lang="ts">
/**
 * 提问导航：把当前会话里用户的每次输入列成一份目录，点击快速定位。
 * 参考 DeepSeek：默认态右侧只剩一排小横线（marker），几乎不占空间；
 * 鼠标经过才弹出概要浮层，点击（marker 或浮层项）定位到该提问。
 *
 * 两种形态（同一份数据、同一套交互）：
 *   rail  —— 宽屏右侧 marker 轨 + hover 浮层（≥1200px）
 *   strip —— 窄屏/窄桌面的横向 chips，贴在对话卡片顶部（触屏没有 hover，功能不丢、只换形态）
 *
 * 只负责展示与交互事件，不碰 store；跳转动作由父组件执行。
 */
import { computed, nextTick, ref } from 'vue'
import { NScrollbar } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    /** 用户消息列表：[{id, text}]，按时间顺序 */
    items: { id: string; text: string }[]
    /** 当前高亮的那条（随消息区滚动自动跟进，或点击后手动指定） */
    activeId?: string | null
    /** 形态：rail 标记轨 + 浮层 / strip 横条 */
    mode?: 'rail' | 'strip'
  }>(),
  { activeId: null, mode: 'rail' },
)
void props

const emit = defineEmits<{
  (e: 'jump', id: string): void
}>()

/** 单条展示文本：压掉换行与多余空白，避免 chips/列表里撑爆布局 */
function brief(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

const hasItems = computed(() => props.items.length > 0)

// ---------------------------------------------------------------- rail 形态
const trackRef = ref<HTMLElement | null>(null)
const tipRef = ref<HTMLElement | null>(null)
const tipVisible = ref(false)
/** 鼠标正悬停的那条：浮层里置顶高亮的就是它（不悬停时回落阅读位 activeId） */
const hoveredId = ref<string | null>(null)
/** 浮层的 fixed 定位样式（left/top/width 由 JS 按 marker 位置算） */
const tipStyle = ref<Record<string, string>>({})
let hideTimer: ReturnType<typeof setTimeout> | null = null

const TIP_WIDTH = 288
const TIP_GAP = 8
const VIEWPORT_MARGIN = 12

function cancelHide() {
  if (hideTimer) {
    clearTimeout(hideTimer)
    hideTimer = null
  }
}

/** 延迟隐藏：给鼠标从 marker 移进浮层留出时间（浮层在轨左侧，中间有缝隙） */
function scheduleHide() {
  cancelHide()
  hideTimer = setTimeout(() => {
    tipVisible.value = false
    hoveredId.value = null
  }, 160)
}

/** 悬停某条提问：弹出浮层，垂直中心对齐该 marker，并把它滚进浮层视野 */
async function showTip(id: string) {
  cancelHide()
  hoveredId.value = id
  tipVisible.value = true
  await nextTick()
  positionTip(id)
  scrollTipItemIntoView(id)
}

function positionTip(id: string) {
  const track = trackRef.value
  const tip = tipRef.value
  if (!track || !tip) return
  const trackRect = track.getBoundingClientRect()
  const marker = track.querySelector(`.qnav-marker[data-qid="${CSS.escape(id)}"]`)
  let top = trackRect.top
  if (marker) {
    const mRect = marker.getBoundingClientRect()
    top = mRect.top + mRect.height / 2 - tip.offsetHeight / 2
  }
  // 钳制在视口内：贴顶/贴底时整体平移，绝不伸出屏幕
  top = Math.max(
    VIEWPORT_MARGIN,
    Math.min(top, window.innerHeight - tip.offsetHeight - VIEWPORT_MARGIN),
  )
  tipStyle.value = {
    left: `${Math.max(VIEWPORT_MARGIN, trackRect.left - TIP_WIDTH - TIP_GAP)}px`,
    top: `${Math.round(top)}px`,
    width: `${TIP_WIDTH}px`,
  }
}

function scrollTipItemIntoView(id: string) {
  const el = tipRef.value?.querySelector(`.q-tip-item[data-qid="${CSS.escape(id)}"]`)
  el?.scrollIntoView({ block: 'nearest' })
}

/** 点击（marker 或浮层项）→ 通知父组件定位，并收起浮层 */
function jump(id: string) {
  emit('jump', id)
  tipVisible.value = false
  hoveredId.value = null
}
</script>

<template>
  <!-- marker 轨：宽屏默认态，一列小横线；hover 弹概要浮层 -->
  <div v-if="mode === 'rail' && hasItems" class="qnav-rail">
    <div ref="trackRef" class="qnav-track" @mouseleave="scheduleHide">
      <button
        v-for="it in items"
        :key="it.id"
        type="button"
        class="qnav-marker"
        :class="{ active: it.id === activeId }"
        :data-qid="it.id"
        :title="brief(it.text)"
        @mouseenter="showTip(it.id)"
        @click="jump(it.id)"
      >
        <span class="qnav-bar" />
      </button>
    </div>

    <!-- 浮层：fixed 定位盖在对话区上，鼠标进入则保留，离开才收 -->
    <div
      v-if="tipVisible"
      ref="tipRef"
      class="qnav-tip"
      :style="tipStyle"
      @mouseenter="cancelHide"
      @mouseleave="scheduleHide"
    >
      <div class="qnav-tip-list">
        <div
          v-for="it in items"
          :key="it.id"
          class="q-tip-item"
          :class="{ active: it.id === (hoveredId || activeId) }"
          :data-qid="it.id"
          @click="jump(it.id)"
        >
          <span class="q-tip-text">{{ brief(it.text) }}</span>
          <span v-if="it.id === (hoveredId || activeId)" class="q-tip-dash" />
        </div>
      </div>
    </div>
  </div>

  <!-- 横条：窄屏/窄桌面，贴在对话卡片顶部 -->
  <div v-else-if="hasItems" class="qstrip">
    <span class="qstrip-label">提问</span>
    <NScrollbar class="qstrip-scroll" x-scrollable>
      <div class="qstrip-row">
        <button
          v-for="(it, i) in items"
          :key="it.id"
          type="button"
          class="qchip"
          :class="{ active: it.id === activeId }"
          :title="brief(it.text)"
          @click="emit('jump', it.id)"
        >
          <span class="qchip-index">{{ i + 1 }}</span>
          {{ brief(it.text) }}
        </button>
      </div>
    </NScrollbar>
  </div>
</template>

<style scoped>
/* ---------------- marker 轨（DeepSeek 式） ---------------- */
.qnav-rail {
  height: 100%;
}

/* marker 沿整条对话高度均匀分布；空间-evenly 保证首尾不贴边 */
.qnav-track {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: space-evenly;
  align-items: center;
  padding: 6px 0;
  overflow: hidden;
}

/* 按钮本身是放大的点击热区，视觉只是一根小横线 */
.qnav-marker {
  flex-shrink: 0;
  width: 18px;
  height: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
}

.qnav-bar {
  width: 10px;
  height: 2px;
  border-radius: 1px;
  background: var(--app-text-3);
  opacity: 0.5;
  transition: width 0.15s, height 0.15s, background 0.15s, opacity 0.15s;
}

.qnav-marker:hover .qnav-bar {
  width: 14px;
  opacity: 0.85;
}

/* 当前阅读位：主色、更长更粗，一眼看到「看到哪了」 */
.qnav-marker.active .qnav-bar {
  width: 16px;
  height: 3px;
  background: var(--app-primary);
  opacity: 1;
}

/* ---------------- hover 浮层 ---------------- */
.qnav-tip {
  position: fixed;
  z-index: 100;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-surface-2);
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}

/* 原生滚动而非 NScrollbar：NScrollbar 根元素不带 scoped 的 data-v-*，
   在 scoped 样式里限高会静默失效（坑 57）；原生 overflow 可配细滚动条 */
.qnav-tip-list {
  max-height: 344px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--app-text-3) transparent;
}

.qnav-tip-list::-webkit-scrollbar {
  width: 4px;
}

.qnav-tip-list::-webkit-scrollbar-thumb {
  background: var(--app-text-3);
  border-radius: 2px;
}

.q-tip-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  cursor: pointer;
  transition: background 0.12s;
}

.q-tip-item:hover {
  background: var(--app-surface-4);
}

/* 单行截断：概要不是全文，一行 + … 最像参考形态 */
.q-tip-text {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--app-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 悬停/阅读位：亮文字 + 右侧小横线指示 */
.q-tip-item.active {
  background: var(--app-surface-4);
}

.q-tip-item.active .q-tip-text {
  color: var(--app-text);
  font-weight: 500;
}

.q-tip-dash {
  flex-shrink: 0;
  width: 10px;
  height: 2px;
  border-radius: 1px;
  background: var(--app-primary);
}

/* ---------------- 横条 ---------------- */
.qstrip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  padding: 8px 0 2px;
}

.qstrip-label {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--app-text-3);
}

.qstrip-scroll {
  flex: 1;
  min-width: 0;
}

.qstrip-row {
  display: flex;
  gap: 6px;
  padding-bottom: 4px;
  width: max-content;
}

.qchip {
  flex-shrink: 0;
  max-width: 220px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border: 1px solid var(--app-border);
  border-radius: 999px;
  background: var(--app-surface-2);
  color: var(--app-text-2);
  font-size: 11.5px;
  line-height: 1.6;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.15s, color 0.15s;
}

.qchip:hover {
  background: var(--app-surface-4);
  color: var(--app-text);
}

.qchip.active {
  background: var(--app-primary-a14);
  border-color: var(--app-primary-a35);
  color: var(--app-text);
}

.qchip-index {
  font-size: 10px;
  color: var(--app-text-3);
}
</style>
