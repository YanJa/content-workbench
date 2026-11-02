<script setup lang="ts">
/**
 * 登录页。渲染在 App 外壳之外（见 App.vue 的未登录分支），
 * 避免工作台的侧栏/轮询在未登录时空跑。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NForm, NFormItem, NInput } from 'naive-ui'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const username = ref('')
const password = ref('')
const err = ref('')
const loading = ref(false)

async function onSubmit() {
  err.value = ''
  // 空值前置校验：浏览器自动填充可能只填了用户名，空密码不打接口
  if (!username.value.trim()) {
    err.value = '请输入用户名'
    return
  }
  if (!password.value) {
    err.value = '请输入密码'
    return
  }
  loading.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    // 登录成功后回到原本想去的页面（守卫带来的 redirect）
    const redirect = (route.query.redirect as string) || '/chat'
    void router.replace(redirect)
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <NCard class="auth-card" :bordered="false">
      <div class="auth-brand">
        <div class="auth-logo">✦</div>
        <div class="auth-title">内容运营台</div>
        <div class="auth-sub">Content Workbench</div>
      </div>

      <NAlert v-if="err" type="error" :bordered="false" style="margin-bottom: 14px">
        {{ err }}
      </NAlert>

      <NForm>
        <NFormItem label="用户名">
          <NInput
            v-model:value="username"
            placeholder="字母 / 数字 / _ - ，3~32 位"
            :input-props="{ name: 'username', autocomplete: 'username' }"
            @keydown.enter="onSubmit"
          />
        </NFormItem>
        <NFormItem label="密码">
          <NInput
            v-model:value="password"
            type="password"
            show-password-on="click"
            placeholder="至少 8 位"
            :input-props="{ name: 'password', autocomplete: 'current-password' }"
            @keydown.enter="onSubmit"
          />
        </NFormItem>
        <NButton type="primary" block :loading="loading" @click="onSubmit">登 录</NButton>
      </NForm>

      <div class="auth-foot">
        还没有账号？<RouterLink to="/register">注册一个</RouterLink>
      </div>
    </NCard>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--app-bg);
  padding: 20px;
}

.auth-card {
  width: 360px;
  background: var(--app-sider-bg);
  border: 1px solid var(--app-border);
}

.auth-brand {
  text-align: center;
  margin-bottom: 20px;
}

.auth-logo {
  width: 44px;
  height: 44px;
  margin: 0 auto 10px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--app-primary), var(--app-purple));
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  color: #fff;
}

.auth-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--app-text);
}

.auth-sub {
  font-size: 11px;
  color: var(--app-text-3);
  margin-top: 2px;
}

.auth-foot {
  margin-top: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--app-text-3);
}

.auth-foot a {
  color: var(--app-primary);
  text-decoration: none;
}

.auth-foot a:hover {
  text-decoration: underline;
}
</style>
