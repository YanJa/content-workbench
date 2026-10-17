<script setup lang="ts">
/**
 * 工作台外壳：侧栏 + 头部 + 内容区（双栏/移动端底栏）+ 新手引导。
 *
 * 只服务于已登录的工作台路由 —— 登录/注册页是顶层路由，不套这层壳
 * （见 router/index.ts）。这样全应用只有一个 RouterView，避免在
 * App.vue 里用 `v-if="isAuthenticated"` 分叉出两个 RouterView：
 * 那会让登录态一变，当前路由组件就被销毁重建（注册后的「接管摘要」
 * 因此被清空，页面退回空表单）。
 */
import { computed, h, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NLayout,
  NLayoutSider,
  NLayoutHeader,
  NLayoutContent,
  NMenu,
  NBadge,
  NButton,
  type MenuOption,
} from 'naive-ui'

import NavIcon from '@/components/NavIcon.vue'
import GuideTour from '@/components/GuideTour.vue'
import { checkHealth } from '@/api/http'
import { useQueueStore } from '@/stores/queue'
import { useAuthStore } from '@/stores/auth'
import { useOnboardingStore } from '@/stores/onboarding'
import { useThemeStore } from '@/stores/theme'

const route = useRoute()
const router = useRouter()
const queue = useQueueStore()
const onboarding = useOnboardingStore()
const auth = useAuthStore()
const theme = useThemeStore()

const collapsed = ref(false)
const isMobile = ref(false)
const llmReady = ref(false)
/** 新手引导：首次自动弹一次，之后靠侧栏/头部按钮手动重看 */
const guideShow = ref(false)

interface NavItem {
  key: string
  icon: string
  label: string
  /** 仅管理员可见 */
  adminOnly?: boolean
  /** 菜单级权限点（见 backend/app/core/rbac.py 的 PERMISSIONS） */
  perm?: string
}

const NAV: NavItem[] = [
  { key: 'chat', icon: 'chat', label: '对话创作', perm: 'menu.chat' },
  { key: 'queue', icon: 'queue', label: '发布队列', perm: 'menu.queue' },
  { key: 'video', icon: 'video', label: '视频生产', perm: 'menu.video' },
  { key: 'usage', icon: 'usage', label: 'AI 用量', perm: 'menu.usage' },
  { key: 'settings', icon: 'activity', label: '系统状态', perm: 'menu.settings' },
  { key: 'admin-users', icon: 'users', label: '用户管理', adminOnly: true },
]

/**
 * 按角色 + 菜单权限过滤后的导航。桌面/移动两处都从这里派生，只写一遍过滤逻辑。
 *
 * 管理员恒通过（`can()` 对 admin 直接返回 true，与后端口径一致）；
 * 权限快照还没回来时（bootstrapped 前）也全显示，避免侧栏先空一下再填满。
 */
const visibleNav = computed(() =>
  NAV.filter((n) => (!n.adminOnly || auth.isAdmin) && (!n.perm || auth.can(n.perm))),
)

/**
 * 菜单 label：发布队列有「已就绪待发布」的记录时，在文字右侧挂一个红点，
 * 不再用「(N)」这种数量 —— 数量会让人误以为是「新消息」，红点只表达
 * 「有东西需要处理」，语义更准。
 */
function queueLabel(n: NavItem) {
  if (n.key === 'queue' && queue.pendingCount > 0) {
    return h('span', { class: 'menu-label-dot' }, [
      n.label,
      h('span', { class: 'dot' }),
    ])
  }
  return n.label
}

const menuOptions = computed<MenuOption[]>(() =>
  visibleNav.value.map((n) => ({
    key: n.key,
    label: () => queueLabel(n),
    icon: () => h(NavIcon, { name: n.icon }),
  })),
)

/** 移动端底部导航直接渲染静态配置，避免用函数式 label 导致渲染异常。 */
const mobileTabs = computed(() =>
  visibleNav.value.map((n) =>
    n.key === 'queue' && queue.pendingCount > 0
      ? { ...n, label: `${n.label} ●` }
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

/** 手动重看引导：清掉「已看过」标记再打开。 */
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

onMounted(async () => {
  isMobile.value = window.innerWidth < 768
  window.addEventListener('resize', () => {
    isMobile.value = window.innerWidth < 768
  })
  try {
    const h = await checkHealth()
    llmReady.value = h.llm_configured
  } catch {
    llmReady.value = false
  }
  void queue.refresh()
  // 首次访问自动跑一遍引导：延迟一拍，等首屏与懒加载页面稳定下来再弹。
  //
  // 但**只在当前停在引导第一站（chat）时才弹**：引导的 positionStep 会
  // router.push 到每一步所在的页面，不分青红皂白地弹，会把 deep-link /
  // 刷新落到 /admin/users、/queue 等页面的用户直接拽回 /chat —— 他正要去
  // 那儿，一段走查先把他劫走了。路由判断放在 setTimeout 回调里（而不是
  // 挂载时），用户这 700ms 内自己导航走也算数。真想看引导的人，侧栏有
  // 「新手引导」按钮手动重看。
  if (!onboarding.done && route.name === 'chat') {
    setTimeout(() => {
      if (!onboarding.done && route.name === 'chat') guideShow.value = true
    }, 700)
  }
})
</script>

<template>
  <NLayout has-sider class="app-layout">
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
                <NavIcon :name="item.icon" :size="20" />
                <span class="tab-label">{{ item.label }}</span>
              </div>
            </div>
          </NLayout>

    <!-- 新手引导：首次自动弹一次，之后从侧栏/头部手动重看 -->
    <GuideTour v-model:show="guideShow" @finish="onGuideFinish" @skip="onGuideSkip" />
  </NLayout>
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

/* 注：认证页（登录/注册）是顶层路由，不套这层壳，
   全屏容器样式见 views/LoginView.vue / RegisterView.vue 的 .auth-page */

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

/* 侧栏菜单「发布队列」红点：表示有已就绪待发布的记录 */
.menu-label-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.menu-label-dot .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #f5222d;
  flex-shrink: 0;
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
