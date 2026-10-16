<script setup lang="ts">
/**
 * 新手引导：聚光灯高亮 + 气泡解说，逐页走一遍系统主线。
 *
 * 为什么自己写而不用组件库：项目用的 naive-ui 2.45 并没有 NTour
 * （只有静态步骤条 NSteps），引一个第三方 tour 库又过重。自研的收益是
 * 完全掌控「只弹一次」的时机、跨页跳转、以及窄屏行为。
 *
 * 实现要点：
 * - 高亮：一个透明框 + `box-shadow: 0 0 0 9999px rgba(0,0,0,.55)` 挖洞，
 *   比「四块遮罩拼」简单，且过渡动画天然顺滑。
 * - 跨页：先 router.push 到目标页，再**轮询等锚点元素出现**（页面是懒加载的，
 *   跳完立刻 querySelector 往往拿不到）。
 * - 兜底：锚点找不到 → 退化成屏幕居中的无高亮卡片，不闪烁、不报错。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton } from 'naive-ui'

import { TOUR_STEPS, type TourStep } from '@/onboarding/steps'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'finish'): void
  (e: 'skip'): void
}>()

const router = useRouter()

const index = ref(0)
const step = computed<TourStep | undefined>(() => TOUR_STEPS[index.value])
const total = TOUR_STEPS.length

// 视口尺寸（resize 时用，触发 rect 重算）
const vpW = ref(typeof window !== 'undefined' ? window.innerWidth : 1280)
const isMobile = computed(() => vpW.value < 768)

// 高亮矩形（相对视口，已含内边距）；null = 无高亮，用居中卡片
const hole = ref<{ top: number; left: number; width: number; height: number } | null>(null)
// 气泡位置（fixed 定位的 left/top + 箭头方向）
const bubble = ref<{ left: number; top: number; arrow: 'top' | 'bottom' | 'left' | 'right' | 'none' }>({
  left: 0,
  top: 0,
  arrow: 'none',
})
// 移动端气泡默认贴底；若高亮目标也贴底（如底部导航），气泡要改贴顶，否则会盖住目标
const dockTop = ref(false)

const PAD = 6 // 高亮框相对目标外扩的像素
const GAP = 14 // 高亮框与气泡的间距
const BUBBLE_W = 320

let rafId: number | null = null
let watchTimer: ReturnType<typeof setInterval> | null = null

function vh() {
  return window.innerHeight
}

/** 清掉所有异步任务（切步/关闭时调用，避免旧任务串场）。 */
function clearTimers() {
  if (watchTimer) {
    clearInterval(watchTimer)
    watchTimer = null
  }
  if (rafId !== null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
}

/** 等元素出现（页面懒加载 + 动画），最多等 ~1.6s。 */
function waitForElement(selector: string, timeout = 1600): Promise<Element | null> {
  return new Promise((resolve) => {
    if (!selector) return resolve(null)
    const found = document.querySelector(selector)
    if (found) return resolve(found)
    const started = Date.now()
    const timer = setInterval(() => {
      const el = document.querySelector(selector)
      if (el || Date.now() - started > timeout) {
        clearInterval(timer)
        resolve(el)
      }
    }, 60)
  })
}

/** 依据目标元素算高亮框 + 气泡位置；target 为 null 时两者都居中。 */
function locate(target: Element | null) {
  if (!target) {
    hole.value = null
    dockTop.value = false
    bubble.value = { left: 0, top: 0, arrow: 'none' }
    return
  }
  const r = target.getBoundingClientRect()
  const h = {
    top: r.top - PAD,
    left: r.left - PAD,
    width: r.width + PAD * 2,
    height: r.height + PAD * 2,
  }
  hole.value = h

  // 窄屏气泡是固定贴底（见 CSS）；若目标落在下半屏，贴底会盖住目标 → 改贴顶
  dockTop.value = isMobile.value && h.top + h.height / 2 > vh() * 0.55

  // 目标过大（占了半屏以上）时，气泡固定贴底，避免被挤出视口
  const tooBig = h.height > vh() * 0.5 || h.width > vpW.value * 0.7
  if (tooBig) {
    bubble.value = {
      left: Math.max(12, (vpW.value - BUBBLE_W) / 2),
      top: vh() - 232,
      arrow: 'none',
    }
    return
  }

  // 自动挑空间最大的一侧
  const spaceTop = h.top
  const spaceBottom = vh() - (h.top + h.height)
  const spaceLeft = h.left
  const spaceRight = vpW.value - (h.left + h.width)
  const want = step.value?.placement ?? 'auto'

  let side: 'top' | 'bottom' | 'left' | 'right'
  if (want !== 'auto') {
    side = want
  } else {
    const best = Math.max(spaceTop, spaceBottom, spaceLeft, spaceRight)
    side = best === spaceBottom ? 'bottom' : best === spaceTop ? 'top' : best === spaceRight ? 'right' : 'left'
  }

  const BH = 200 // 气泡估算高度，用于夹取不越界
  if (side === 'bottom' || side === 'top') {
    const top = side === 'bottom' ? h.top + h.height + GAP : h.top - GAP - BH
    bubble.value = {
      left: Math.min(Math.max(12, h.left + h.width / 2 - BUBBLE_W / 2), vpW.value - BUBBLE_W - 12),
      top: Math.min(Math.max(12, top), vh() - BH - 12),
      arrow: side === 'bottom' ? 'top' : 'bottom',
    }
  } else {
    const left = side === 'right' ? h.left + h.width + GAP : h.left - GAP - BUBBLE_W
    bubble.value = {
      left: Math.min(Math.max(12, left), vpW.value - BUBBLE_W - 12),
      top: Math.min(Math.max(12, h.top + h.height / 2 - BH / 2), vh() - BH - 12),
      arrow: side === 'right' ? 'left' : 'right',
    }
  }
}

/** 定位当前步：必要时跳转 → 等元素 → 滚进视野 → 计算位置。 */
async function positionStep() {
  const s = step.value
  if (!s) return
  // 1) 需要先换页
  if (s.route && router.currentRoute.value.name !== s.route) {
    await router.push({ name: s.route })
  }
  // 2) 等锚点（懒加载页面需要时间）
  const selector = isMobile.value ? s.anchorMobile ?? s.anchor : s.anchor
  let target: Element | null = null
  if (selector) {
    target = await waitForElement(selector)
    if (target) {
      // 目标在视口外先滚进来，再等一帧让滚动稳定后取 rect
      const r = target.getBoundingClientRect()
      if (r.top < 0 || r.bottom > vh()) {
        target.scrollIntoView({ block: 'center', behavior: 'smooth' })
        await new Promise((res) => setTimeout(res, 260))
      }
    }
  }
  locate(target)
  // 3) 开滚动/尺寸监听，保持高亮贴合（capture 才能收到内部滚动容器的 scroll）
  attachFollow(target)
}

/** 目标因滚动/缩放而动时重算（rAF 节流）。 */
let followedTarget: Element | null = null
function attachFollow(target: Element | null) {
  followedTarget = target
  if (watchTimer) clearInterval(watchTimer)
  // 用低频轮询而非 scroll 监听：页面里有多个滚动容器，轮询更省心，
  // 且 60ms 的位移肉眼几乎察觉不到（比挂一堆 capture 监听更稳）。
  watchTimer = setInterval(() => {
    if (rafId !== null) return
    rafId = requestAnimationFrame(() => {
      rafId = null
      const t = followedTarget
      if (t && document.body.contains(t)) locate(t)
    })
  }, 60)
}

async function goto(n: number) {
  if (n < 0 || n >= total) return
  clearTimers()
  hole.value = null
  index.value = n
  await nextTick()
  await positionStep()
}

function next() {
  if (index.value >= total - 1) return finish()
  void goto(index.value + 1)
}

function prev() {
  if (index.value <= 0) return
  void goto(index.value - 1)
}

function finish() {
  clearTimers()
  emit('update:show', false)
  emit('finish')
}

function skip() {
  clearTimers()
  emit('update:show', false)
  emit('skip')
}

function onKeydown(e: KeyboardEvent) {
  if (!props.show) return
  if (e.key === 'Escape') {
    e.preventDefault()
    skip()
  } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
    e.preventDefault()
    next()
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    prev()
  }
}

function onResize() {
  vpW.value = window.innerWidth
  void positionStep()
}

watch(
  () => props.show,
  (v) => {
    if (v) {
      index.value = 0
      window.addEventListener('resize', onResize)
      window.addEventListener('keydown', onKeydown, true)
      void goto(0)
    } else {
      clearTimers()
      followedTarget = null
      window.removeEventListener('resize', onResize)
      window.removeEventListener('keydown', onKeydown, true)
    }
  },
)

onBeforeUnmount(() => {
  clearTimers()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', onKeydown, true)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="show && step"
      class="guide-root"
      role="dialog"
      aria-modal="true"
      :aria-label="`新手引导，第 ${index + 1} 步，共 ${total} 步`"
    >
      <!-- 高亮框：透明 + 超大 box-shadow 挖洞 -->
      <div
        v-if="hole"
        class="guide-hole"
        :style="{
          top: hole.top + 'px',
          left: hole.left + 'px',
          width: hole.width + 'px',
          height: hole.height + 'px',
        }"
      />
      <!-- 无高亮时的整屏遮罩 -->
      <div v-else class="guide-mask" />

      <!-- 气泡解说 -->
      <div
        class="guide-bubble"
        :class="[`arrow-${bubble.arrow}`, { center: !hole, 'dock-top': dockTop }]"
        :style="
          hole
            ? { left: bubble.left + 'px', top: bubble.top + 'px', width: BUBBLE_W + 'px' }
            : {}
        "
        tabindex="-1"
        @keydown="onKeydown"
      >
        <div class="gb-head">
          <span class="gb-title">{{ step.title }}</span>
          <span class="gb-count">{{ index + 1 }} / {{ total }}</span>
        </div>
        <div class="gb-body">{{ step.body }}</div>
        <div class="gb-foot">
          <NButton size="tiny" quaternary class="gb-skip" @click="skip">跳过引导</NButton>
          <div class="gb-nav">
            <NButton v-if="index > 0" size="small" @click="prev">上一步</NButton>
            <NButton size="small" type="primary" @click="next">
              {{ index === total - 1 ? '完成' : '下一步' }}
            </NButton>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.guide-root {
  position: fixed;
  inset: 0;
  z-index: 3000;
}

/* 挖洞法的遮罩：靠高亮框的超大 box-shadow 铺满四周 */
.guide-hole {
  position: fixed;
  border-radius: 10px;
  box-shadow:
    0 0 0 9999px rgba(0, 0, 0, 0.55),
    0 0 0 2px var(--app-primary);
  transition:
    top 0.22s ease,
    left 0.22s ease,
    width 0.22s ease,
    height 0.22s ease;
  pointer-events: none;
}

/* 无高亮目标时的整屏遮罩 */
.guide-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
}

.guide-bubble {
  position: fixed;
  background: var(--app-surface);
  color: var(--app-text);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 14px 16px;
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.32);
  outline: none;
  transition:
    top 0.22s ease,
    left 0.22s ease;
}

/* 无高亮时居中 */
.guide-bubble.center {
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: min(420px, 90vw);
}

.gb-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.gb-title {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--app-text);
}

.gb-count {
  font-size: 11.5px;
  color: var(--app-text-3);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.gb-body {
  font-size: 13px;
  line-height: 1.75;
  color: var(--app-text-2);
  white-space: pre-line;
}

.gb-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 14px;
}

.gb-nav {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

/* 箭头：用旋转的小方块模拟（自带背景色，跟气泡一致） */
.guide-bubble::after {
  content: '';
  position: absolute;
  width: 10px;
  height: 10px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  transform: rotate(45deg);
}
.arrow-top::after {
  top: -6px;
  left: 28px;
  border-right: none;
  border-bottom: none;
}
.arrow-bottom::after {
  bottom: -6px;
  left: 28px;
  border-left: none;
  border-top: none;
}
.arrow-left::after {
  left: -6px;
  top: 22px;
  border-right: none;
  border-top: none;
}
.arrow-right::after {
  right: -6px;
  top: 22px;
  border-left: none;
  border-bottom: none;
}
.guide-bubble.center::after {
  display: none;
}

@media (max-width: 768px) {
  .guide-bubble {
    left: 12px !important;
    right: 12px;
    width: auto !important;
    top: auto !important;
    bottom: calc(14px + env(safe-area-inset-bottom));
  }
  /* 目标是底部导航等贴底元素时，气泡改贴顶，避免盖住高亮目标 */
  .guide-bubble.dock-top {
    bottom: auto !important;
    top: calc(14px + env(safe-area-inset-top));
  }
  .guide-bubble.center {
    left: 12px !important;
    right: 12px;
    top: auto !important;
    bottom: 40%;
    transform: none;
  }
  .guide-bubble::after {
    display: none;
  }
}
</style>
