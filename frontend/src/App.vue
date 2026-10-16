<script setup lang="ts">
/**
 * 应用根组件：主题 provider + 唯一的一个 RouterView。
 *
 * 这里刻意**不做**登录态分支 —— 登录/注册页是顶层路由，工作台路由挂在
 * AppShell 这层壳下面（见 router/index.ts）。全应用只此一个 RouterView：
 * 早期版本在「未登录/已登录」两个分支里各放了一个，结果登录态一变，
 * 当前路由组件就被销毁重建（注册成功后的「存量接管摘要」因此被清空、
 * 页面退回空表单）。认证页与外壳的隔离交给路由层级，而不是 v-if 分叉。
 */
import { computed } from 'vue'

import {
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  darkTheme,
  lightTheme,
  zhCN,
  dateZhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'

import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()

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
        <RouterView />
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>
