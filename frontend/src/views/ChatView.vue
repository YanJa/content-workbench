<script setup lang="ts">
/**
 * 对话创作页：多轮对话 + 流式输出 + 工具调用 + 人工确认。
 *
 * 这是整个工作台的核心页面，也是「AI Agent 嵌入」的落地形态：
 * 用户用自然语言提需求 → AI 生成内容 → 按需调用工具落库/推送。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NCard,
  NDrawer,
  NDrawerContent,
  NInput,
  NSelect,
  NSpace,
  NTag,
  NSpin,
  NScrollbar,
  NAlert,
  NModal,
  NForm,
  NFormItem,
  useMessage,
} from 'naive-ui'

import { useChatStore } from '@/stores/chat'
import { useQueueStore } from '@/stores/queue'
import { useAuthStore } from '@/stores/auth'
import ActionBtn from '@/components/ActionBtn.vue'
import { usePermGate } from '@/composables/usePermGate'
import { estimateUsage } from '@/api/usage'
import { saveToQueue } from '@/api/queue'
import type { UsageBreakdown } from '@/api/usage'
import type { Platform } from '@/types'
import MarkdownView from '@/components/MarkdownView.vue'
import ConvList from '@/components/ConvList.vue'
import QuestionNav from '@/components/QuestionNav.vue'

const chat = useChatStore()
const queue = useQueueStore()
const auth = useAuthStore()
const message = useMessage()

/**
 * 发送消息的门控：按钮级权限 `chat.send` + 配额 `chat`。
 *
 * 配额超限时后端会在 SSE **开流之前**返回 429（状态码改不了才是真麻烦），
 * 所以这里提前把按钮灰掉，用户不用等一个必然失败的请求。
 */
const sendGate = usePermGate({ perm: 'chat.send', quota: 'chat' })
const router = useRouter()
const route = useRoute()

/**
 * 每条回复的费用。
 *
 * 为什么要在前端再调一次估算接口：计价规则（峰谷倍数、长上下文档位、
 * 模型别名）全在后端 pricing 引擎里，前端绝不复刻一份 —— 复刻了迟早
 * 跟后端算出两个数，用户就会开始怀疑所有数字。
 * 这里只做「拿到 tokens → 换钱 → 缓存到消息上」，避免重复请求。
 */
const costCache = ref<Record<string, UsageBreakdown>>({})

async function loadCost(msgId: string, usage: { prompt_tokens: number; completion_tokens: number }) {
  if (costCache.value[msgId]) return
  try {
    const r = await estimateUsage({
      prompt: usage.prompt_tokens,
      completion: usage.completion_tokens,
    })
    costCache.value = { ...costCache.value, [msgId]: r }
  } catch {
    // 估算失败不影响对话本身，静默忽略（不弹错，避免每条消息都报一次）
  }
}

watch(
  () => chat.messages.filter((m) => m.usage && !m.streaming && !costCache.value[m.id]).length,
  () => {
    for (const m of chat.messages) {
      if (m.usage && !m.streaming) void loadCost(m.id, m.usage)
    }
  },
  { immediate: true },
)

function fmtCost(v: number): string {
  if (!v) return '≈ ¥0'
  if (v < 0.0001) return '≈ ¥0.0000'
  return `≈ ¥${v.toFixed(v < 0.01 ? 4 : 3)}`
}

const input = ref('')
const scrollRef = ref<InstanceType<typeof NScrollbar> | null>(null)

const platformOptions = [
  { label: '通用对话', value: '' },
  { label: '📰 公众号', value: 'gzh' },
  { label: '📕 小红书', value: 'xiaohongshu' },
  { label: '🎵 抖音', value: 'douyin' },
]

const scene = ref<string>('')

// ---- 会话重命名 / 删除（列表 UI 在 ConvList 里，这里只处理数据动作）----
async function onRename(id: string, title: string) {
  try {
    await chat.renameConversation(id, title)
  } catch {
    message.error('重命名失败，请重试')
  }
}

async function doRemove(id: string) {
  try {
    await chat.removeConversation(id)
    message.success('会话已删除')
  } catch {
    message.error('删除失败，请重试')
  }
}

// ---- 移动端：会话列表改为抽屉，窄屏也需要能查看/切换历史会话 ----
const isMobile = ref(false)
const drawerShow = ref(false)
/** 窄桌面（<1200px）：右侧提问导航栏收成横向 chips，功能不丢、只换形态 */
const isNarrow = ref(false)

function syncViewport() {
  isMobile.value = window.innerWidth < 768
  isNarrow.value = window.innerWidth < 1200
}

/** 选中会话：加载后自动收起抽屉（用户确认的交互）。 */
async function onPickConversation(id: string) {
  await chat.openConversation(id)
  initReasoningFolded()
  drawerShow.value = false
}

function onNewConversation() {
  chat.newConversation()
  drawerShow.value = false
}

/**
 * 思维链折叠状态（用户确认的方案）：
 *   流式过程中默认展开——推理过程实时流出，是新用户最想看的东西；
 *   打开历史会话默认折叠——回显时先看正文，想看的再点开；
 *   用户手动点过之后以用户选择为准（切会话不清手动项）。
 */
const collapsed = ref<Record<string, boolean>>({})
function toggleReasoning(id: string) {
  collapsed.value[id] = !collapsed.value[id]
}
/** 打开历史会话后调用：把带思维链的消息预置为折叠（不动用户手动切过的）。 */
function initReasoningFolded() {
  for (const m of chat.messages) {
    if (m.reasoning && collapsed.value[m.id] === undefined) {
      collapsed.value[m.id] = true
    }
  }
}

// ---------------------------------------------------------------- 提问导航
/** 当前会话里用户的每次输入（右侧导航栏 / 窄屏横条的数据源） */
const userTurns = computed(() =>
  chat.messages.filter((m) => m.role === 'user').map((m) => ({ id: m.id, text: m.content })),
)
/** 当前高亮的提问：点击跳转后手动指定，或随消息区滚动自动跟进 */
const activeTurnId = ref<string | null>(null)
/** 跳转/反跳时的临时高亮消息 id，2.5s 后淡出 */
const highlightMsgId = ref('')
let highlightTimer: ReturnType<typeof setTimeout> | null = null

function flashMessage(id: string) {
  highlightMsgId.value = id
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => {
    highlightMsgId.value = ''
  }, 2500)
}

/** 滚动定位到某条消息（等 DOM 就绪后做，跳转与数据加载有竞态） */
async function scrollToMessage(id: string, flash = true) {
  if (!id) return
  await nextTick()
  // 轮询等选择器出现：历史消息是异步加载的，router.push 返回时往往还没渲染
  const deadline = Date.now() + 2000
  for (;;) {
    const el = document.querySelector(`.msg[data-msg-id="${CSS.escape(id)}"]`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      if (flash) flashMessage(id)
      return
    }
    if (Date.now() > deadline) return
    await new Promise((r) => setTimeout(r, 60))
  }
}

/** 点提问导航：滚到那条用户消息（连带看到它的 AI 回复） */
function onJumpToTurn(id: string) {
  activeTurnId.value = id
  void scrollToMessage(id)
}

/**
 * 消息区滚动 → 自动高亮「当前看到的那条提问」。
 * 只认 user 消息：assistant 的 id 不在导航目录里，若把它算进去，
 * 「没滚动」场景下 current 会落在最后一条 assistant 上，active 永远匹配不上。
 * 取容器内最后一个已滚过顶部的 user 消息；120ms 节流，够跟手又不刷屏。
 */
let scrollTick = 0
function onMsgScroll() {
  const now = Date.now()
  if (now - scrollTick < 120) return
  scrollTick = now
  const box = document.querySelector('.msg-scroll')
  if (!box) return
  const top = box.getBoundingClientRect().top
  const nodes = box.querySelectorAll<HTMLElement>('.msg-user[data-msg-id]')
  let current: string | null = null
  nodes.forEach((n) => {
    if (n.getBoundingClientRect().top <= top + 80) {
      current = n.dataset.msgId ?? null
    }
  })
  if (current) activeTurnId.value = current
}

// 消息 DOM 变了但未必产生滚动事件（流式追加、切换会话、历史加载），
// 主动跟一次 active——否则「当前看到哪条提问」会停在旧值甚至空值。
watch(() => chat.messages.length, () => nextTick(onMsgScroll))
watch(() => chat.currentId, () => nextTick(onMsgScroll))

const canSend = computed(
  () =>
    input.value.trim().length > 0 &&
    !chat.streaming &&
    // 权限不够 / 配额用尽都不给发。禁用态下 tooltip 说明原因，
    // 别让人点了个灰按钮还不知道为什么。
    !sendGate.blocked.value,
)

/** 发送按钮的禁用提示：空串 = 不禁用 */
const sendBlockTip = computed(() => (canSend.value ? '' : sendGate.reason.value))

/**
 * 快捷指令：降低上手成本。
 * 注意这里每条都必须是「能自洽的整句」——点击后会直接发送，
 * 带「……」占位符的句子发出去只会得到 AI 的一句反问。
 */
const quickPrompts = computed(() => {
  if (scene.value === 'gzh')
    return [
      '帮我写一篇公众号文章，主题是新手如何开始做内容运营',
      '把「选题比产量重要」这个观点扩写成一篇 2000 字长文',
      '给「内容运营入门指南」这篇文章起 5 个标题',
    ]
  if (scene.value === 'xiaohongshu')
    return [
      '写一篇小红书笔记，分享我做内容运营的 3 个心得',
      '把「每日选题灵感库」写成一篇种草文案',
      '围绕「职场效率」帮我想 10 个话题标签',
    ]
  if (scene.value === 'douyin')
    return [
      '写一条 60 秒口播脚本，讲普通人怎么做内容选题',
      '把「如何开始做内容运营」拆成 3 个分镜',
      '给「内容运营」主题写前 3 秒的钩子，要抓人',
    ]
  return ['帮我写一篇公众号文章', '队列里现在有什么？', '这个月的内容选题有什么建议？']
})

async function onSend() {
  const text = input.value.trim()
  if (!text || chat.streaming || sendGate.blocked.value) return
  input.value = ''
  chat.platform = (scene.value || null) as Platform | null
  // 乐观置灰：配额是「提交时预扣」，请求发出去就已经记上了。
  // 万一后端其实拒了（429），http 拦截器会刷新 /auth/me 把数字纠正回来。
  auth.markUsed('chat')
  void chat.send(text, (scene.value || null) as Platform | null)
}

/**
 * 快捷指令：点一下直接发出去。
 *
 * 之前用 NTag + @click —— NTag 并不声明 click 事件，只能靠 attr 透传，
 * 行为不稳；而且「填进输入框」这个效果太轻，用户会觉得点了没反应。
 * 换成 NButton（原生 button，天生可点、可聚焦、可键盘触发）+ 直接发送。
 */
function useQuickPrompt(q: string) {
  if (chat.streaming) {
    message.warning('正在生成中，请稍候再点')
    return
  }
  input.value = q
  void onSend()
}

function onKeydown(e: KeyboardEvent) {
  // Enter 发送，Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void onSend()
  }
}

async function onConfirm(idx: number) {
  const msg = chat.messages[idx]
  if (!msg?.pendingConfirm || !chat.currentId) return
  try {
    await chat.confirm(chat.currentId, msg.pendingConfirm, idx)
    message.success('操作已执行')
    void queue.refresh()
  } catch (e) {
    message.error(`执行失败：${(e as Error).message}`)
  }
}

function onReject(idx: number) {
  chat.reject(idx)
  message.info('已取消')
}

// ---------------------------------------------------------------- 保存到队列
// 对话创作只产出纯内容，排版加工在队列页做。所以这里只负责「把这条 AI 回复
// 存进队列」，不做任何排版。这是唯一的入队入口（AI 已不允许自动入队）。
const saveModalShow = ref(false)
const savingMsg = ref<{ id: string; content: string } | null>(null)
const savePlatform = ref<Platform>('gzh')
const saveTitle = ref('')
const saveSubmitting = ref(false)
/** msg.id → content_id：记录哪些消息已保存到队列，操作栏据此显示「去队列」 */
const savedMap = ref<Record<string, string>>({})

/** 从 Markdown 正文里猜一个标题：优取 `# 标题`，其次取第一行非空文本。 */
function guessTitle(md: string): string {
  const lines = md.split('\n').map((l) => l.trim()).filter(Boolean)
  for (const l of lines) {
    const m = l.match(/^#{1,6}\s+(.+)$/)
    if (m) return m[1].trim().slice(0, 60)
  }
  return (lines[0] ?? '').replace(/^[>#*\-\s]+/, '').slice(0, 40) || '未命名内容'
}

/** 摘要：优先取 `**摘要**：xxx`，否则取第一段正文前 120 字。 */
function guessSummary(md: string): string {
  const m = md.match(/\*\*摘要\*\*\s*[:：]\s*(.+)/)
  if (m) return m[1].trim().slice(0, 120)
  const paras = md
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#') && !l.startsWith('>') && !l.startsWith('**摘要'))
  return (paras[0] ?? '').replace(/[*`\-]/g, '').slice(0, 120)
}

function openSaveModal(msg: { id: string; content: string }) {
  savingMsg.value = { id: msg.id, content: msg.content }
  savePlatform.value = (scene.value || 'gzh') as Platform
  saveTitle.value = guessTitle(msg.content)
  saveModalShow.value = true
}

async function doSaveToQueue() {
  const msg = savingMsg.value
  if (!msg) return
  if (!saveTitle.value.trim()) {
    message.warning('先给内容起个标题')
    return
  }
  saveSubmitting.value = true
  try {
    const res = await saveToQueue({
      platform: savePlatform.value,
      title: saveTitle.value.trim(),
      body: msg.content,
      summary: guessSummary(msg.content),
      kind: 'text',
      conversation_id: chat.currentId,
      msg_id: msg.id,
    })
    if (res.ok) {
      savedMap.value[msg.id] = res.content_id
      saveModalShow.value = false
      void queue.refresh()
      message.success('已保存到发布队列（待加工）')
    } else {      message.error('保存失败')
    }
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    saveSubmitting.value = false
  }
}

/** 跳到发布队列并定位到这条内容（保存成功后的快捷入口）。
 *  带上来源会话/消息，队列页才能给出「返回对话」形成闭环。 */
function goQueue(contentId: string, msgId?: string) {
  router.push({
    name: 'queue',
    query: {
      content_id: contentId,
      from_conversation_id: chat.currentId ?? undefined,
      from_msg_id: msgId,
    },
  })
}

/** 从队列页「返回对话」跳回来：清掉来源标记，避免刷新重复触发 */
function backToQueue() {
  router.push({ name: 'queue', query: { content_id: returnContentId.value || undefined } })
}

/** 从队列跳来时带的来源内容 id（用于「返回发布队列」提示条） */
const returnContentId = ref('')

async function copyMessage(content: string) {
  const ok = await navigator.clipboard
    .writeText(content)
    .then(() => true)
    .catch(() => false)
  message[ok ? 'success' : 'warning'](ok ? '已复制' : '复制失败，请手动选择复制')
}

async function scrollToBottom() {
  await nextTick()
  scrollRef.value?.scrollTo({ top: 999999, behavior: 'smooth' })
}

watch(
  () => chat.messages.length,
  () => void scrollToBottom(),
)
watch(
  () => chat.messages[chat.messages.length - 1]?.content,
  () => void scrollToBottom(),
)

onMounted(async () => {
  syncViewport()
  window.addEventListener('resize', syncViewport)
  void chat.loadConversations()
  await restoreFromQueue()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', syncViewport)
  if (highlightTimer) clearTimeout(highlightTimer)
})

/**
 * 从队列页「回到对话」跳来：打开来源会话并定位到那条 AI 回复。
 * URL：/chat?conversation_id=<convId>&msg_id=<msgId>&content_id=<cid>
 *
 * 兜底：会话可能已被删除（接口 404）—— 不白屏，回到空会话并提示。
 */
async function restoreFromQueue() {
  const convId = route.query.conversation_id as string | undefined
  if (!convId) return
  returnContentId.value = (route.query.content_id as string | undefined) ?? ''
  try {
    await chat.openConversation(convId)
    initReasoningFolded()
  } catch {
    chat.newConversation()
    message.warning('来源会话已不存在，已为你打开新会话')
    void router.replace({ name: 'chat' })
    return
  }
  const msgId = route.query.msg_id as string | undefined
  if (msgId) {
    activeTurnId.value = msgId
    await scrollToMessage(msgId)
  }
  // 清掉 query：刷新不再重复定位，但保留「返回发布队列」所需的内容 id
  void router.replace({ name: 'chat' })
}
</script>

<template>
  <div class="chat-page">
    <!-- 左侧：会话列表（桌面常驻；窄屏改用抽屉，见页面底部 NDrawer） -->
    <NCard v-if="!isMobile" class="conv-panel" size="small" :bordered="true">
      <ConvList
        :conversations="chat.conversations"
        :current-id="chat.currentId"
        @select="onPickConversation"
        @new="chat.newConversation()"
        @rename="onRename"
        @remove="doRemove"
      />
    </NCard>

    <!-- 右侧：对话主区 -->
    <div class="chat-main">
      <!-- 从队列页「回到对话」跳来时的闭环入口：一键回去看刚保存的内容 -->
      <div v-if="returnContentId" class="chat-return-bar">
        <NButton size="tiny" quaternary @click="backToQueue">← 返回发布队列</NButton>
        <span class="chat-return-hint">回到刚保存的那条内容</span>
      </div>

      <NCard class="chat-card" size="small">
        <template #header>
          <div class="chat-toolbar">
            <!-- 移动端入口：窄屏下会话列表在抽屉里，这里给个开关 -->
            <NButton
              v-if="isMobile"
              class="conv-toggle"
              size="small"
              quaternary
              @click="drawerShow = true"
            >
              ☰ 会话
            </NButton>
            <NSelect
              v-model:value="scene"
              :options="platformOptions"
              size="small"
              style="width: 150px"
              :disabled="chat.streaming"
            />
            <span class="toolbar-hint">
              平台会决定 AI 的写作规则与格式；留空则为通用对话
            </span>
            <div class="toolbar-spacer" />
            <NButton
              v-if="chat.streaming"
              size="small"
              type="warning"
              quaternary
              @click="chat.stop()"
            >
              停止生成
            </NButton>
          </div>
        </template>

        <!-- 消息区 -->
        <!-- 窄屏/窄桌面：提问导航收成卡片顶部的横条（flex-shrink:0，不占消息区高度） -->
        <QuestionNav
          v-if="isNarrow"
          mode="strip"
          :items="userTurns"
          :active-id="activeTurnId"
          @jump="onJumpToTurn"
        />
        <NScrollbar ref="scrollRef" class="msg-scroll" @scroll="onMsgScroll">
          <!-- 空态：不要用 NEmpty 的画布（它会渲染自带的「无数据」占位图），
               首屏欢迎内容自己排版更干净 -->
          <div v-if="!chat.messages.length" class="welcome">
            <div class="welcome-box">
              <div class="welcome-title">开始创作</div>
              <div class="welcome-sub">
                描述你想做什么，AI 会按所选平台的规范生成内容。<br />
                生成完成后可以一键保存到发布队列。
              </div>
              <div class="quick-list">
                <NButton
                  v-for="q in quickPrompts"
                  :key="q"
                  class="quick-btn"
                  size="small"
                  quaternary
                  round
                  @click="useQuickPrompt(q)"
                >
                  {{ q }}
                </NButton>
              </div>
              <div class="quick-hint">点任意一个就直接开始生成</div>
            </div>
          </div>

          <div
            v-for="(m, idx) in chat.messages"
            :key="m.id"
            class="msg"
            :class="[`msg-${m.role}`, { 'msg-flash': m.id === highlightMsgId }]"
            :data-msg-id="m.id"
          >
            <div class="msg-role">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <div class="msg-body">
              <!-- 工具调用过程 -->
              <div v-if="m.toolCalls?.length" class="tool-trace">
                <div v-for="t in m.toolCalls" :key="t.id" class="tool-row">
                  <NTag size="tiny" :bordered="false" type="info">工具</NTag>
                  <span class="tool-name">{{ t.name }}</span>
                  <span v-if="Object.keys(t.arguments || {}).length" class="tool-args">
                    {{ JSON.stringify(t.arguments).slice(0, 60) }}
                  </span>
                  <NTag
                    v-if="t.result"
                    size="tiny"
                    :bordered="false"
                    :type="t.result?.error ? 'error' : 'success'"
                  >
                    {{ t.result?.error ? '失败' : '完成' }}
                  </NTag>
                  <NSpin v-else :size="12" />
                </div>
              </div>

              <!-- 思维链：推理模型先想后答，可折叠 -->
              <div v-if="m.reasoning" class="reasoning-box">
                <div class="reasoning-head" @click="toggleReasoning(m.id)">
                  <span class="reasoning-icon">{{ collapsed[m.id] ? '▸' : '▾' }}</span>
                  <span>思考过程</span>
                  <span class="reasoning-len">{{ m.reasoning.length }} 字</span>
                  <span v-if="m.streaming && !m.content" class="reasoning-live">推理中…</span>
                </div>
                <div v-show="!collapsed[m.id]" class="reasoning-body">{{ m.reasoning }}</div>
              </div>

              <!-- 正文：AI 回复按 Markdown 渲染，用户消息保持纯文本 -->
              <div class="msg-content">
                <MarkdownView v-if="m.role === 'assistant' && !m.streaming" :content="m.content" />
                <template v-else>{{ m.content }}</template>
                <span v-if="m.streaming" class="cursor">▍</span>
              </div>

              <!-- AI 回复的操作栏：复制 / 保存到队列（排版在队列页做） -->
              <div v-if="m.role === 'assistant' && !m.streaming && m.content" class="msg-actions">
                <ActionBtn name="copy" tip="复制" @click="copyMessage(m.content)" />
                <ActionBtn name="inbox" tip="保存到队列" type="primary" @click="openSaveModal(m)" />
                <ActionBtn
                  v-if="savedMap[m.id]"
                  name="eye"
                  tip="去队列查看"
                  type="info"
                  @click="goQueue(savedMap[m.id], m.id)"
                />
              </div>

              <!-- token 用量 + 本条花费（费用由后端计价引擎算，前端不复刻规则） -->
              <div v-if="m.usage && !m.streaming" class="usage-line">
                tokens {{ m.usage.total_tokens }}
                （入 {{ m.usage.prompt_tokens }} / 出 {{ m.usage.completion_tokens }}<template
                  v-if="m.usage.reasoning_tokens"
                > · 思维链 {{ m.usage.reasoning_tokens }}</template>）
                <template v-if="costCache[m.id]">
                  ·
                  <span class="cost" :title="costCache[m.id].is_peak ? '高峰时段，单价翻倍' : '空闲时段'">
                    {{ fmtCost(costCache[m.id].cost) }}
                    <template v-if="costCache[m.id].is_peak">· 高峰</template>
                    <template v-if="!costCache[m.id].known">
                      · 按 {{ costCache[m.id].model }} 估价
                    </template>
                  </span>
                </template>
              </div>

              <!-- 错误 -->
              <NAlert v-if="m.error" type="error" :bordered="false" class="msg-alert">
                {{ m.error }}
              </NAlert>

              <!-- 危险操作确认 -->
              <NAlert
                v-if="m.pendingConfirm"
                type="warning"
                :bordered="false"
                class="msg-alert"
              >
                <template #header>需要确认</template>
                <div class="confirm-body">
                  <div class="confirm-msg">{{ m.pendingConfirm.message }}</div>
                  <div class="confirm-detail">
                    <code>{{ m.pendingConfirm.name }}</code>
                    <pre>{{ JSON.stringify(m.pendingConfirm.arguments, null, 2) }}</pre>
                  </div>
                  <NSpace style="margin-top: 10px">
                    <NButton size="small" type="primary" @click="onConfirm(idx)">
                      确认执行
                    </NButton>
                    <NButton size="small" @click="onReject(idx)">取消</NButton>
                  </NSpace>
                </div>
              </NAlert>
            </div>
          </div>
        </NScrollbar>

        <!-- 输入区 -->
        <div class="composer">
          <NInput
            v-model:value="input"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            placeholder="描述你的需求，Enter 发送，Shift+Enter 换行"
            :disabled="chat.streaming"
            @keydown="onKeydown"
          />
          <NButton
            type="primary"
            class="send-btn"
            :disabled="!canSend"
            :loading="chat.streaming"
            @click="onSend"
          >
            {{ sendGate.left.value === null ? '发送' : `发送（剩 ${sendGate.left.value}）` }}
          </NButton>
        </div>
        <!-- 被权限/配额挡住时说清楚为什么。刻意用常驻文字而不是 tooltip：
             禁用按钮上的 hover 提示在多数浏览器里根本不触发，用户会以为
             按钮坏了而不是自己没权限。 -->
        <div v-if="sendBlockTip" class="send-blocked-tip">⚠ {{ sendBlockTip }}</div>
      </NCard>
    </div>

    <!-- 右侧：提问导航（DeepSeek 式 marker 轨：默认只剩一排小横线，hover 才弹概要浮层） -->
    <div v-if="!isNarrow" class="qnav-rail-wrap">
      <QuestionNav
        mode="rail"
        :items="userTurns"
        :active-id="activeTurnId"
        @jump="onJumpToTurn"
      />
    </div>

    <!-- 保存到队列：确认平台与标题后入库（排版在队列页做） -->
    <NModal
      v-model:show="saveModalShow"
      preset="card"
      title="保存到发布队列"
      style="max-width: 480px"
    >
      <NForm label-placement="left" label-width="72">
        <NFormItem label="平台">
          <NSelect
            v-model:value="savePlatform"
            :options="[
              { label: '📰 公众号', value: 'gzh' },
              { label: '📕 小红书', value: 'xiaohongshu' },
              { label: '🎵 抖音', value: 'douyin' },
            ]"
          />
        </NFormItem>
        <NFormItem label="标题">
          <NInput v-model:value="saveTitle" placeholder="给这份内容起个标题" />
        </NFormItem>
      </NForm>
      <div class="hint" style="margin-top: -4px">
        保存后进入发布队列（状态：待加工），去队列页做排版、配图，再推公众号草稿箱。
      </div>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="saveModalShow = false">取消</NButton>
          <NButton type="primary" :loading="saveSubmitting" @click="doSaveToQueue">
            确认保存
          </NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- 移动端会话抽屉：窄屏下从这里查看/切换/新建历史会话 -->
    <NDrawer v-model:show="drawerShow" :width="280" placement="left">
      <NDrawerContent closable>
        <template #header>
          <div class="drawer-head">
            <span>会话</span>
            <NButton size="tiny" type="primary" @click="onNewConversation">新建</NButton>
          </div>
        </template>
        <ConvList
          :conversations="chat.conversations"
          :current-id="chat.currentId"
          :show-header="false"
          @select="onPickConversation"
          @new="onNewConversation"
          @rename="onRename"
          @remove="doRemove"
        />
      </NDrawerContent>
    </NDrawer>
  </div>
</template>

<style scoped>
/* 桌面三列：会话栏 | 对话主区 | 提问导航轨（marker 轨只占 ~20px， hover 才展开） */
.chat-page {
  display: grid;
  grid-template-columns: 240px 1fr 20px;
  /* 单行 + minmax(0,1fr)：行高被锁定成「容器高度」而非「内容高度」，
     否则消息一多，grid 行会被内容顶高，整页跟着变长 */
  grid-template-rows: minmax(0, 1fr);
  gap: 16px;
  height: calc(100vh - 130px);
}

/* 窄桌面（<1200px）：收回右侧提问导航栏（竖栏改横条，见卡片内 QuestionNav） */
@media (max-width: 1200px) {
  .chat-page {
    grid-template-columns: 240px 1fr;
  }
}

/* 桌面侧栏容器；列表本体样式在 ConvList.vue 里（桌面/移动共用一份） */
.conv-panel {
  overflow: hidden;
}

/* 右侧提问导航轨容器：极窄列，只放一列 marker；浮层是 fixed 定位不受这里裁剪 */
.qnav-rail-wrap {
  min-width: 0;
  padding: 2px 0;
}

/* 从队列跳回来的闭环入口条：贴在对话卡片上方，不占卡片内部高度 */
.chat-return-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  padding: 0 2px 8px;
}

.chat-return-hint {
  font-size: 11.5px;
  color: var(--app-text-3);
}

/* 移动端「会话」开关按钮 */
.conv-toggle {
  flex-shrink: 0;
}

/* 抽屉标题栏：左标题右「新建」，与桌面侧栏 header 视觉一致 */
.drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-right: 24px;
}

/* 主区：把卡片的高度链坐实（min-height:0 才能让内部的滚动区正确收缩） */
.chat-main {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.chat-card {
  /* flex:1 + min-height:0 比 height:100% 稳：height:100% 需要父级有确定高度，
     一旦链路中有一环是 auto 就会退化成「按内容撑高」 */
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/**
 * 让输入框钉在卡片底部，而不是被推到视口外。
 *
 * naive-ui 的 NCard 会把内容包一层 `.n-card-content`：卡片根元素虽然是 column
 * flex，但那层内容体本身是 `display:block` —— 里面的 `.msg-scroll{flex:1}` 在
 * block 父级里毫无意义，于是消息区按自然高度撑开、把 `.composer` 挤到卡片外面
 * （实测 900 高的视口下溢出 72px，要下拉才看得到输入框）。
 * 把这层也变成 column flex + min-height:0，flex 链才真正贯通：
 * 消息区占满剩余高度并自己滚动，输入框始终钉在卡片内、完整可见。
 *
 * 注意：是「钉住 + 留白」，不是「紧贴到 0px」。高度链坐实之后输入框本来就
 * 在视口内，不需要靠删 padding 去省空间；保留卡片自带的 --n-padding-bottom
 * （默认约 12px），输入框与卡片底边之间才有呼吸感。
 */
.chat-card :deep(.n-card-content) {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.toolbar-hint {
  font-size: 11.5px;
  color: var(--app-text-3);
}

.toolbar-spacer {
  flex: 1;
}

/* 消息区吃满剩余高度并自己滚动。
   必须用 :deep()：NScrollbar 渲染出的根元素**不携带 scoped 的 data-v 属性**
   （实测根元素只有 class/role/style），所以普通的 `.msg-scroll` 作用域选择器
   根本匹配不上，flex:1 形同虚设 —— 消息一多就把卡片撑高、输入框被顶出视口。 */
.chat-card :deep(.msg-scroll) {
  flex: 1;
  min-height: 0;
  padding-right: 6px;
}

.welcome-box {
  text-align: center;
  padding: 40px 20px;
}

.welcome-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 8px;
}

.welcome-sub {
  font-size: 12.5px;
  color: var(--app-text-3);
  line-height: 1.8;
  margin-bottom: 18px;
}

.quick-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}

.quick-btn {
  background: var(--app-surface-2);
  color: var(--app-text-2);
}

.quick-btn:hover {
  background: var(--app-surface-4);
  color: var(--app-text);
}

.quick-hint {
  margin-top: 10px;
  font-size: 11.5px;
  color: var(--app-text-3);
}

.msg {
  display: grid;
  grid-template-columns: 34px 1fr;
  gap: 10px;
  padding: 12px 0;
  border-bottom: 1px solid var(--app-border-50);
  /* scrollIntoView 定位时离滚动容器顶部留点空间，不贴边 */
  scroll-margin-top: 12px;
  border-radius: 6px;
}

/* 跳转定位时的闪烁高亮（提问导航点击 / 队列反跳），2.5s 后由 JS 清 class */
.msg-flash {
  animation: msg-flash 2.4s ease-out;
}

@keyframes msg-flash {
  0%,
  55% {
    background: var(--app-primary-a16);
  }
  100% {
    background: transparent;
  }
}

.msg-role {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11.5px;
  font-weight: 600;
  background: var(--app-surface-2);
  color: var(--app-text-2);
}

.msg-user .msg-role {
  background: var(--app-primary-a18);
  color: var(--app-primary-soft);
}

.msg-body {
  min-width: 0;
}

.msg-content {
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--app-text);
  white-space: pre-wrap;
  word-break: break-word;
}

/* MarkdownView 内部自带块级排版，别再叠加 pre-wrap 的空白语义 */
.msg-content :deep(.md-body) {
  white-space: normal;
}

.msg-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  opacity: 0.55;
  transition: opacity 0.15s;
}
.msg:hover .msg-actions {
  opacity: 1;
}

/* 思维链盒子：视觉上弱化，不抢正文的注意力 */
.reasoning-box {
  margin-bottom: 10px;
  border-left: 2px solid var(--app-primary-soft-a35);
  background: var(--app-primary-soft-a06);
  border-radius: 0 6px 6px 0;
  overflow: hidden;
}

.reasoning-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-size: 12px;
  color: var(--app-text-2);
  cursor: pointer;
  user-select: none;
}

.reasoning-head:hover {
  color: var(--app-text);
}

.reasoning-icon {
  font-size: 10px;
}

.reasoning-len {
  opacity: 0.6;
}

.reasoning-live {
  color: var(--app-primary-soft);
  animation: pulse 1.4s ease-in-out infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}

.reasoning-body {
  max-height: 220px;
  overflow-y: auto;
  padding: 0 10px 8px 28px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.usage-line {
  margin-top: 8px;
  font-size: 11px;
  color: var(--app-text-3);
  font-variant-numeric: tabular-nums;
}

.usage-line .cost {
  color: var(--app-green);
  cursor: help;
}

.cursor {
  color: var(--app-primary);
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.msg-alert {
  margin-top: 10px;
}

.confirm-body {
  font-size: 12.5px;
}

.confirm-msg {
  margin-bottom: 8px;
}

.confirm-detail code {
  color: var(--app-primary-soft);
  font-size: 12px;
}

.confirm-detail pre {
  margin-top: 6px;
  padding: 10px;
  background: var(--app-sunken);
  border-radius: 8px;
  font-size: 11.5px;
  line-height: 1.6;
  overflow-x: auto;
  color: var(--app-text-2);
  max-height: 180px;
}

.tool-trace {
  margin-bottom: 8px;
  padding: 8px 10px;
  background: var(--app-sunken);
  border-radius: 8px;
  border-left: 2px solid var(--app-primary);
}

.tool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 3px 0;
}

.tool-name {
  color: var(--app-primary-soft);
  font-family: 'SF Mono', Menlo, Consolas, monospace;
}

.tool-args {
  color: var(--app-text-3);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  padding-top: 14px;
  border-top: 1px solid var(--app-border);
  margin-top: 12px;
}

.send-btn {
  flex-shrink: 0;
  height: 40px;
}

.send-blocked-tip {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: #d03050;
}

@media (max-width: 768px) {
  .chat-page {
    grid-template-columns: 1fr;
    height: calc(100vh - 140px);
  }
  /* 窄屏桌面侧栏不占列 —— 实际由 v-if="!isMobile" 移除，这里只是兜底 */
  .conv-panel {
    display: none;
  }
  .toolbar-hint {
    display: none;
  }
  .conv-toggle {
    margin-right: -4px;
  }
  .composer {
    flex-direction: column;
    align-items: stretch;
  }
  .send-btn {
    width: 100%;
  }
}
</style>
