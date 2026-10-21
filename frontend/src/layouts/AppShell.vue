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
  NDropdown,
  useDialog,
  type MenuOption,
} from 'naive-ui'

import NavIcon from '@/components/NavIcon.vue'
import GuideTour from '@/components/GuideTour.vue'
import ChangelogModal from '@/components/ChangelogModal.vue'
import { checkHealth } from '@/api/http'
import { useQueueStore } from '@/stores/queue'
import { useAuthStore } from '@/stores/auth'
import { useChatStore } from '@/stores/chat'
import { useOnboardingStore } from '@/stores/onboarding'
import { useThemeStore } from '@/stores/theme'
import { useVersionStore } from '@/stores/version'

const route = useRoute()
const router = useRouter()
const queue = useQueueStore()
const onboarding = useOnboardingStore()
const auth = useAuthStore()
const chat = useChatStore()
const theme = useThemeStore()
const version = useVersionStore()
const dialog = useDialog()

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
  { key: 'revenue', icon: 'revenue', label: '收益看板', perm: 'menu.revenue' },
  { key: 'settings', icon: 'activity', label: '系统设置', perm: 'menu.settings' },
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

/**
 * 移动端底部导航配置。与桌面端一样用独立红点表达「有待发布」，
 * 但**不能**像早期那样把 ● 拼进 label 字符串——底栏 tab 只有
 * 屏宽/7（375px 下约 53px），「发布队列●」直接换行，60px 高的底栏
 * 被撑爆成图标+两行错行文字（2026-10-21 用户截图复现）。
 * 现在红点是绝对定位在图标右上角的独立元素，不占文档流。
 */
const mobileTabs = computed(() =>
  visibleNav.value.map((n) =>
    n.key === 'queue' && queue.pendingCount > 0 ? { ...n, dot: true } : { ...n, dot: false },
  ),
)

const activeKey = computed(() => (route.name as string) ?? 'chat')
const pageTitle = computed(() => (route.meta?.title as string) ?? '')
const pageDesc = computed(() => (route.meta?.desc as string) ?? '')

function onMenuSelect(key: string) {
  void router.push({ name: key })
}

/**
 * 退出登录：先弹确认框再清令牌回登录页。
 *
 * 退出是不可撤销操作（未提交的对话输入、页面上正在编辑的内容都会丢），
 * 误触代价不小，不能一点就直接退。
 */
function onLogout() {
  dialog.warning({
    title: '退出登录',
    content: '确定要退出当前账号吗？',
    positiveText: '退出',
    negativeText: '取消',
    onPositiveClick: () => {
      // chat store 整体重置：否则下个用户登录进来会看到上家的对话
      // （会话列表是按 owner 拉的所以正常，只有对话区残留 —— 2026-10-19 实测）
      chat.reset()
      auth.logout()
      void router.replace('/login')
    },
  })
}

/**
 * 顶栏用户区：头像 + 昵称（回落用户名）合成一个下拉入口，
 * 「个人中心」与「退出登录」收在里面 —— 顶栏就那么大点地方，
 * 摊两个按钮既挤又和信息架构上的「用户级功能」不搭。
 */
const userDisplayName = computed(() => auth.user?.nickname || auth.user?.username || '')

const userAvatarText = computed(() => (userDisplayName.value[0] || '?').toUpperCase())

const userMenuOptions: MenuOption[] = [
  { key: 'profile', label: '个人中心' },
  { key: 'logout', label: '退出登录' },
]

function onUserMenuSelect(key: string) {
  if (key === 'profile') {
    void router.push({ name: 'profile' })
  } else if (key === 'logout') {
    onLogout()
  }
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
    // 初始化版本 store：h.version 有则以后端为准，没有则 store 内部兜底到
    // 前端 APP_VERSION —— 后端旧代码/未重启时不至于让版本号空着。
    version.init(h.version)
  } catch {
    llmReady.value = false
    // 探活失败也兜底初始化：至少展示前端已知版本，别让关于卡片只剩 "v"
    version.init()
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
            <!-- 侧栏底部：随时可重看引导 / 看更新日志 -->
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
              <NButton
                quaternary
                size="small"
                class="guide-entry"
                :title="collapsed ? '更新日志' : undefined"
                @click="version.openChangelog()"
              >
                <span class="guide-entry-icon">📋</span>
                <span v-if="!collapsed">更新日志</span>
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
                <NDropdown :options="userMenuOptions" trigger="hover" @select="onUserMenuSelect">
                  <NButton size="small" quaternary class="user-trigger">
                    <img
                      v-if="auth.user?.avatar_url"
                      class="user-avatar"
                      :src="auth.user.avatar_url"
                      alt="头像"
                    />
                    <span v-else class="user-avatar">{{ userAvatarText }}</span>
                    <span class="user-name" :title="`账号：${auth.user?.username ?? ''}`">
                      {{ userDisplayName }}
                    </span>
                  </NButton>
                </NDropdown>
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
                  title="更新日志"
                  @click="version.openChangelog()"
                >
                  📋
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
                <span class="tab-icon-wrap">
                  <NavIcon :name="item.icon" :size="20" />
                  <span v-if="item.dot" class="tab-dot" />
                </span>
                <span class="tab-label">{{ item.label }}</span>
              </div>
            </div>
          </NLayout>

    <!-- 新手引导：首次自动弹一次，之后从侧栏/头部手动重看 -->
    <GuideTour v-model:show="guideShow" @finish="onGuideFinish" @skip="onGuideSkip" />

    <!-- 新功能提醒（版本升级后自动弹） -->
    <ChangelogModal
      v-model:show="version.whatsNewOpen"
      :entries="version.newEntries"
      title="新功能"
      @close="version.dismissWhatsNew()"
    />
    <!-- 更新日志：手动入口，看全部历史 -->
    <ChangelogModal
      v-model:show="version.changelogOpen"
      :entries="version.allEntries"
      title="更新日志"
    />
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

/* 侧栏底部：常驻「新手引导」+「更新日志」入口（折叠时只剩图标） */
.sider-foot {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 12px;
  padding: 0 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
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

/* 头部当前账号（昵称优先，回落用户名）+ 退出（用户模块） */
.user-name {
  font-size: 12px;
  color: var(--app-text-2);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 用户下拉触发器：头像 + 名字，hover 出菜单 */
.user-trigger {
  align-items: center;
}

.user-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  flex-shrink: 0;
  background: linear-gradient(135deg, var(--app-primary), var(--app-purple));
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  /* 上传的头像图：裁好的方图，cover 保平安（极端比例下也不变形） */
  object-fit: cover;
  /* ⚠️ 间距必须写在这层，不能写 .user-trigger 的 gap —— naive-ui 的
     NButton 内部还有一层 .n-button__content 才是 flex 父级，设在外层
     button 上的 gap 是死代码（computed 有值但不生效，2026-10-19 实测
     visual_gap=0 才发现）。margin 在头像自己身上，与结构无关。 */
  margin-right: 14px;
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
  min-width: 0;
}

/* 图标定位容器：红点以它为基准，不占文档流、不挤压文字 */
.tab-icon-wrap {
  position: relative;
  display: inline-flex;
  line-height: 1;
}

/* 底部栏「发布队列」红点：有待发布记录时挂在图标右上角 */
.tab-dot {
  position: absolute;
  top: -1px;
  right: -4px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #f5222d;
  flex-shrink: 0;
}

.tab-label {
  /* tab 只有屏宽/7，文字绝不换行——长了就省略号，绝不把底栏撑高 */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
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
  /* 窄屏头部空间紧张：标题允许截断省略，右侧按钮收紧间距，
     否则「对话创作」和「AI 已就绪」会互相挤压重叠 */
  .header-left {
    gap: 8px;
    flex: 1;
  }
  .header-left h1 {
    font-size: 15px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 1;
  }
  .header-right {
    gap: 2px;
  }
  .status-text {
    padding: 2px 2px;
    white-space: nowrap;
  }
  .app-content {
    padding: 12px 10px calc(76px + env(safe-area-inset-bottom));
  }
}
</style>
