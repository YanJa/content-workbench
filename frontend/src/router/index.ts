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
          meta: { title: '对话创作', desc: '和 AI 一起把想法变成成品', icon: '◈' },
        },
        {
          path: 'queue',
          name: 'queue',
          component: () => import('@/views/QueueView.vue'),
          meta: { title: '发布队列', desc: '集中管理成品与发布状态', icon: '▤' },
        },
        {
          path: 'video',
          name: 'video',
          component: () => import('@/views/VideoView.vue'),
          meta: { title: '视频生产', desc: '脚本转视频，异步渲染', icon: '🎬' },
        },
        {
          path: 'usage',
          name: 'usage',
          component: () => import('@/views/UsageView.vue'),
          meta: { title: 'AI 用量', desc: '花了多少钱、花在哪儿', icon: '▣' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { title: '系统状态', desc: '服务健康、工具清单、配置检查', icon: '⚙' },
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
  }
}

/**
 * 全局守卫：未登录进工作台 → 登录页（带 redirect 回来）；
 * 已登录访问登录/注册页 → 回工作台；
 * 非管理员访问管理页 → 回对话页。
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
    return { name: 'chat' }
  }
  return true
})

router.afterEach((to) => {
  const t = to.meta?.title as string | undefined
  if (t) document.title = `${t} · 内容运营工作台`
})

export default router
