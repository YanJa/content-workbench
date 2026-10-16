<script setup lang="ts">
/**
 * 应用外壳：深浅双主题 + 布局 + 导航。
 * 主题由 stores/theme.ts 管理（light/dark/system），
 * 这里把 resolved 主题喂给 Naive UI 的 ConfigProvider，
 * 同时 <html data-theme> 切换我们自己的 CSS 变量（styles/theme.css）。
 */
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NConfigProvider,
  NMessageProvider,
  NDialogProvider,
  NLayout,
  NLayoutSider,
  NLayoutHeader,
  NLayoutContent,
  NMenu,
  NBadge,
  NButton,
  darkTheme,
  lightTheme,
  zhCN,
  dateZhCN,
  type GlobalThemeOverrides,
  type MenuOption,
} from 'naive-ui'

import NavIcon from '@/components/NavIcon.vue'
import GuideTour from '@/components/GuideTour.vue'
import { checkHealth } from '@/api/http'
import { useQueueStore } from '@/stores/queue'
import { useThemeStore } from '@/stores/theme'
import { useOnboardingStore } from '@/stores/onboarding'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const queue = useQueueStore()
const theme = useThemeStore()
const onboarding = useOnboardingStore()
const auth = useAuthStore()

const llmReady = ref(false)
const collapsed = ref(false)
const isMobile = ref(false)

// 新手引导：首次自动弹一次，之后靠侧栏/头部按钮手动重看
const guideShow = ref(false)

/** 手动重看：清掉「已看过」标记再打开。 */
function replayGuide() {
  onboarding.reset()
  guideShow.value = true
}

function onGuideFinish() {
  onboarding.markDone()
}

function onGuideSkip() {
  // 主动跳过也算看过，不再骚扰
  onboarding.markDone()
}

/** 暗色主题变量：延续 demo 期的暗色视觉，主色用蓝紫 */
const darkOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#4F7CFF',
    primaryColorHover: '#3D6BFF',
    primaryColorPressed: '#2F5AE8',
    borderRadius: '10px',
    fontSize: '14px',
    bodyColor: '#0F1117',
    cardColor: '#171A23',
    modalColor: '#171A23',
    popoverColor: '#1E222D',
    borderColor: '#282D3A',
    textColorBase: '#E6E9F0',
    textColor1: '#E6E9F0',
    textColor2: '#9BA3B4',
    textColor3: '#6B7385',
  },
}

/** 亮色主题变量：同一套语义，对应浅色值 */
const lightOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#3B6BFF',
    primaryColorHover: '#2F5AE8',
    primaryColorPressed: '#2448C7',
    borderRadius: '10px',
    fontSize: '14px',
    bodyColor: '#F5F6F8',
    cardColor: '#FFFFFF',
    modalColor: '#FFFFFF',
    popoverColor: '#FFFFFF',
    borderColor: '#E3E6EC',
    textColorBase: '#1A1D26',
    textColor1: '#1A1D26',
    textColor2: '#5C6373',
    textColor3: '#8B93A7',
  },
}

const naiveTheme = computed(() => (theme.isDark ? darkTheme : lightTheme))
const themeOverrides = computed(() => (theme.isDark ? darkOverrides : lightOverrides))

/** 导航单一数据源：桌面侧栏和移动底栏都由它派生，避免两处重复定义。
 *  icon 是 NavIcon 的 name——「系统状态」用脉冲线而非齿轮，因为页面本职是健康巡检。 */
const NAV = [
  { key: 'chat', icon: 'chat', label: '对话创作' },
  { key: 'queue', icon: 'queue', label: '发布队列' },
  { key: 'video', icon: 'video', label: '视频生产' },
  { key: 'usage', icon: 'usage', label: 'AI 用量' },
  { key: 'settings', icon: 'activity', label: '系统状态' },
]

const menuOptions = computed<MenuOption[]>(() =>
  NAV.map((n) => ({
    key: n.key,
    label:
      n.key === 'queue' && queue.pendingCount > 0
        ? `发布队列 (${queue.pendingCount})`
        : n.label,
    icon: () => h(NavIcon, { name: n.icon }),
  })),
)

/** 移动端底部导航直接渲染静态配置，避免用函数式 label 导致渲染异常。 */
const mobileTabs = computed(() =>
  NAV.map((n) =>
    n.key === 'queue' && queue.pendingCount > 0
      ? { ...n, label: `队列 ${queue.pendingCount}` }
      : n,
  ),
)

const activeKey = computed(() => (route.name as string) ?? 'chat')
const pageTitle = computed(() => (route.meta?.title as string) ?? '')
const pageDesc = computed(() => (route.meta?.desc as string) ?? '')

function onMenuSelect(key: string) {
  void router.push({ name: key })
}

/** 退出登录：清令牌回登录页（守卫会把后续工作台访问都挡在登录页外）。 */
function onLogout() {
  auth.logout()
  void router.replace('/login')
}

onMounted(async () => {
  isMobile.value = window.innerWidth < 768
  window.addEventListener('resize', () => {
    isMobile.value = window.innerWidth < 768
  })
  // 未登录时探活/轮询只会拿 401，等登录后由 LoginView 跳转进来再跑
  if (!auth.isAuthenticated) return
  try {
    const h = await checkHealth()
    llmReady.value = h.llm_configured
  } catch {
    llmReady.value = false
  }
  void queue.refresh()
  // 首次访问自动跑一遍引导：延迟一拍，等首屏与懒加载页面稳定下来再弹
  if (!onboarding.done) {
    setTimeout(() => {
      if (!onboarding.done) guideShow.value = true
    }, 700)
  }
})
</script>

<template>
  <NConfigProvider
    :theme="naiveTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <NMessageProvider>
      <NDialogProvider>
        <!-- 未登录：登录/注册页全屏渲染在工作台外壳之外 -->
        <div v-if="!auth.isAuthenticated" class="auth-shell">
          <RouterView />
        </div>
        <NLayout v-else has-sider class="app-layout">
          <NLayoutSider
            v-if="!isMobile"
            bordered
            collapse-mode="width"
            :collapsed-width="64"
            :width="220"
            :collapsed="collapsed"
            show-trigger
            @collapse="collapsed = true"
            @expand="collapsed = false"
            class="app-sider"
          >
            <div class="brand">
              <div class="brand-logo">✦</div>
              <div v-if="!collapsed">
                <div class="brand-name">内容运营台</div>
                <div class="brand-sub">Content Workbench</div>
              </div>
            </div>
            <NMenu
              :collapsed="collapsed"
              :collapsed-width="64"
              :collapsed-icon-size="20"
              :options="menuOptions"
              :value="activeKey"
              @update:value="onMenuSelect"
            />
            <!-- 侧栏底部：随时可重看引导 -->
            <div class="sider-foot">
              <NButton
                quaternary
                size="small"
                class="guide-entry"
                :title="collapsed ? '新手引导' : undefined"
                @click="replayGuide"
              >
                <span class="guide-entry-icon">❓</span>
                <span v-if="!collapsed">新手引导</span>
              </NButton>
            </div>
          </NLayoutSider>

          <NLayout>
            <NLayoutHeader bordered class="app-header">
              <div class="header-left">
                <h1>{{ pageTitle }}</h1>
                <span class="header-desc">{{ pageDesc }}</span>
              </div>
              <div class="header-right">
                <NBadge
                  :type="llmReady ? 'success' : 'warning'"
                  dot
                  :offset="[-2, 2]"
                >
                  <span class="status-text">
                    {{ llmReady ? 'AI 已就绪' : 'AI 未配置' }}
                  </span>
                </NBadge>
                <span class="user-name" :title="`账号：${auth.user?.username ?? ''}`">
                  {{ auth.user?.username }}
                </span>
                <NButton size="small" quaternary @click="onLogout">退出</NButton>
                <NButton
                  size="small"
                  quaternary
                  :title="theme.isDark ? '切换到浅色主题' : '切换到深色主题'"
                  @click="theme.toggle()"
                >
                  {{ theme.isDark ? '☀' : '☾' }}
                </NButton>
                <NButton
                  v-if="isMobile"
                  size="small"
                  quaternary
                  title="新手引导"
                  @click="replayGuide"
                >
                  ❓
                </NButton>
                <NButton
                  v-if="isMobile"
                  size="small"
                  quaternary
                  @click="router.push('/settings')"
                >
                  ⚙
                </NButton>
              </div>
            </NLayoutHeader>

            <NLayoutContent class="app-content">
              <RouterView />
            </NLayoutContent>

            <!-- 移动端底部导航 -->
            <div v-if="isMobile" class="mobile-tabbar">
              <div
                v-for="item in mobileTabs"
                :key="item.key"
                class="tab"
                :class="{ active: activeKey === item.key }"
                @click="onMenuSelect(item.key)"
              >
                <span class="tab-icon">{{ item.icon }}</span>
                <span class="tab-label">{{ item.label }}</span>
              </div>
            </div>
          </NLayout>
        </NLayout>

        <!-- 新手引导：首次自动弹一次，之后从侧栏/头部手动重看 -->
        <GuideTour
          v-if="auth.isAuthenticated"
          v-model:show="guideShow"
          @finish="onGuideFinish"
          @skip="onGuideSkip"
        />
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>

<style scoped>
.app-layout {
  height: 100vh;
}

.app-sider {
  background: var(--app-sider-bg);
  /* 底部「新手引导」用绝对定位贴着底边 */
  position: relative;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid var(--app-border);
}

.brand-logo {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 9px;
  background: linear-gradient(135deg, var(--app-primary), var(--app-purple));
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  color: #fff;
}

.brand-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  letter-spacing: 0.3px;
}

.brand-sub {
  font-size: 11px;
  color: var(--app-text-3);
}

/* 侧栏底部：常驻「新手引导」入口（折叠时只剩 ❓） */
.sider-foot {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 12px;
  padding: 0 12px;
  display: flex;
  justify-content: center;
}

.guide-entry {
  width: 100%;
  justify-content: center;
  color: var(--app-text-2);
}

.guide-entry-icon {
  font-size: 13px;
}

.app-header {
  height: 62px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--app-sider-bg);
}

.header-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}

.header-left h1 {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text);
  white-space: nowrap;
}

.header-desc {
  font-size: 12px;
  color: var(--app-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.status-text {
  font-size: 12px;
  color: var(--app-text-2);
  padding: 2px 6px;
}

/* 头部当前账号名 + 退出（用户模块） */
.user-name {
  font-size: 12px;
  color: var(--app-text-2);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 未登录时的全屏容器（登录/注册页） */
.auth-shell {
  min-height: 100vh;
  background: var(--app-bg);
}

.app-content {
  padding: 20px 24px 32px;
  overflow-y: auto;
  background: var(--app-bg);
}

.mobile-tabbar {
  display: flex;
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: 60px;
  background: var(--app-tabbar-bg);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-top: 1px solid var(--app-border);
  z-index: 100;
  padding-bottom: env(safe-area-inset-bottom);
}

.tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  font-size: 11px;
  color: var(--app-text-3);
  cursor: pointer;
  user-select: none;
  -webkit-tap-highlight-color: transparent;
  transition: color 0.15s;
}

.tab-icon {
  font-size: 17px;
  line-height: 1;
}

.tab.active {
  color: var(--app-primary);
}

@media (max-width: 768px) {
  .app-header {
    height: 54px;
    padding: 0 12px;
  }
  .header-desc {
    display: none;
  }
  .app-content {
    padding: 12px 10px calc(76px + env(safe-area-inset-bottom));
  }
}
</style>
