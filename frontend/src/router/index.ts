import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 认证页是**顶层**路由：不套工作台外壳，也不走 AppShell 的布局。
    // 这样全应用只有一个 RouterView（在 App.vue 里），登录态切换不会
    // 把当前路由组件销毁重建。
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { title: '注册', public: true },
    },
    // 工作台：全部挂在 AppShell 这层壳下
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        { path: '', redirect: '/chat' },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '对话创作', desc: '和 AI 一起把想法变成成品', icon: '◈', perm: 'menu.chat' },
        },
        {
          path: 'queue',
          name: 'queue',
          component: () => import('@/views/QueueView.vue'),
          meta: { title: '发布队列', desc: '集中管理成品与发布状态', icon: '▤', perm: 'menu.queue' },
        },
        {
          path: 'video',
          name: 'video',
          component: () => import('@/views/VideoView.vue'),
          meta: { title: '视频生产', desc: '脚本转视频，异步渲染', icon: '🎬', perm: 'menu.video' },
        },
        {
          path: 'assets',
          name: 'assets',
          component: () => import('@/views/AssetsView.vue'),
          meta: {
            title: '素材库',
            desc: '图片素材集中管理，写文章时直接取用',
            icon: '🖼',
            perm: 'menu.assets',
          },
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: () => import('@/views/KnowledgeView.vue'),
          meta: {
            title: '知识库',
            desc: '给 AI 的素材库，写稿时自动参考',
            icon: '◍',
            perm: 'menu.knowledge',
          },
        },
        {
          path: 'skills',
          name: 'skills',
          component: () => import('@/views/SkillsView.vue'),
          meta: {
            title: '技能',
            desc: '教 AI 一套具体做法，匹配时自动照做',
            icon: '✦',
            perm: 'menu.skills',
          },
        },
        {
          path: 'usage',
          name: 'usage',
          component: () => import('@/views/UsageView.vue'),
          meta: { title: 'AI 用量', desc: '花了多少钱、花在哪儿', icon: '▣', perm: 'menu.usage' },
        },
        {
          path: 'revenue',
          name: 'revenue',
          component: () => import('@/views/RevenueView.vue'),
          meta: {
            title: '收益看板',
            desc: '流量主广告收益、曝光与趋势',
            icon: '¥',
            perm: 'menu.revenue',
          },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: {
            title: '系统设置',
            desc: '服务健康、对话通道、凭证与环境变量',
            icon: '⚙',
            perm: 'menu.settings',
          },
        },
        {
          path: 'models',
          name: 'model-pool',
          component: () => import('@/views/ModelPoolView.vue'),
          meta: {
            title: '模型池',
            desc: '多家厂商的模型、额度与功能绑定',
            icon: '◫',
          },
        },
        // 个人中心：**刻意不进侧栏菜单**。它是按用户走的（顶栏头像下拉进入），
        // 不是按功能模块走的 —— 侧栏那一列是「活儿」，这里是「我是谁」。
        // 也不挂 perm：所有人都该能改自己的资料。
        {
          path: 'profile',
          name: 'profile',
          component: () => import('@/views/ProfileView.vue'),
          meta: { title: '个人中心', desc: '账号资料、作者信息与个人配额', icon: '👤' },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: () => import('@/views/AdminUsersView.vue'),
          meta: {
            title: '用户管理',
            desc: '建账号、改角色、停用与全站用量',
            icon: '⚙',
            adminOnly: true,
          },
        },
      ],
    },
  ],
})

// 让 TS 认识 meta.adminOnly（vue-router 的 RouteMeta 默认不含自定义字段）
declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    desc?: string
    icon?: string
    public?: boolean
    /**
     * 仅管理员可见。这只是「提前挡一道」——真正的权限校验在后端，
     * 非管理员直接调 /api/admin/* 一样是 403。
     */
    adminOnly?: boolean
    /**
     * 进入这个页面需要的菜单权限点（如 'menu.video'）。
     * 管理员在用户管理里取消勾选某一项，对应菜单就从侧栏消失、直接敲
     * 网址也会被弹到有权限的页面。同样是「提前挡一道」，页面里的按钮
     * 还会各自校验按钮级权限。
     */
    perm?: string
  }
}

/** 路由名 → 菜单权限点。用于「跳去一个他有权限看的页面」。 */
const MENU_PERM: Record<string, string> = {
  chat: 'menu.chat',
  queue: 'menu.queue',
  assets: 'menu.assets',
  knowledge: 'menu.knowledge',
  skills: 'menu.skills',
  video: 'menu.video',
  usage: 'menu.usage',
  revenue: 'menu.revenue',
  settings: 'menu.settings',
}

/**
 * 全局守卫：未登录进工作台 → 登录页（带 redirect 回来）；
 * 已登录访问登录/注册页 → 回工作台；
 * 非管理员访问管理页 / 没有对应菜单权限 → 弹到有权限的页面。
 * bootstrapped 为 false 只可能是挂载前的极短窗口（main.ts 保证
 * bootstrap 先于 mount 完成），放行即可。
 */
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!auth.bootstrapped) return true
  if (to.meta?.public) {
    return auth.isAuthenticated ? { name: 'chat' } : true
  }
  if (!auth.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta?.adminOnly && !auth.isAdmin) {
    return redirectToPermitted(auth, to.name as string)
  }
  const perm = to.meta?.perm ?? MENU_PERM[to.name as string]
  if (perm && !auth.can(perm)) {
    return redirectToPermitted(auth, to.name as string)
  }
  return true
})

/**
 * 找一个用户有权限看的页面跳过去。
 *
 * 刻意**不在「全都没权限」时强行回 /chat**：那会和上面的 perm 校验打起
 * 来（回 /chat → 又没 menu.chat → 再回 /chat，无限循环）。全都没权限时
 * 就放行让人进去 —— 侧栏是空的很难看，但页面里的按钮会各自校验，
 * 而且至少还能退出登录。
 */
function redirectToPermitted(auth: ReturnType<typeof useAuthStore>, from: string) {
  for (const [name, perm] of Object.entries(MENU_PERM)) {
    if (name !== from && auth.can(perm)) return { name }
  }
  return true
}

router.afterEach((to) => {
  const t = to.meta?.title as string | undefined
  if (t) document.title = `${t} · 内容运营工作台`
})

export default router
