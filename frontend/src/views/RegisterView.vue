<script setup lang="ts">
/**
 * 注册页。邀请码输入框常驻：首账号免邀请码（后端放行并认领存量数据），
 * 之后的账号由后端按 AUTH_REGISTER_MODE 判定，提示语以服务端返回为准。
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
const confirmPassword = ref('')
const inviteCode = ref('')
const err = ref('')
const loading = ref(false)

async function onSubmit() {
  err.value = ''
  if (password.value !== confirmPassword.value) {
    err.value = '两次输入的密码不一致'
    return
  }
  loading.value = true
  try {
    await auth.register(username.value.trim(), password.value, inviteCode.value.trim())
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
        <div class="auth-title">注册账号</div>
        <div class="auth-sub">第一个注册的账号将认领全部存量数据</div>
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
            :input-props="{ name: 'new-password', autocomplete: 'new-password' }"
            @keydown.enter="onSubmit"
          />
        </NFormItem>
        <NFormItem label="确认密码">
          <NInput
            v-model:value="confirmPassword"
            type="password"
            show-password-on="click"
            placeholder="再输一次"
            :input-props="{ name: 'confirm-password', autocomplete: 'new-password' }"
            @keydown.enter="onSubmit"
          />
        </NFormItem>
        <NFormItem label="邀请码">
          <NInput
            v-model:value="inviteCode"
            placeholder="首账号留空即可；其余账号需要管理员配的邀请码"
            :input-props="{ name: 'invite-code' }"
            @keydown.enter="onSubmit"
          />
        </NFormItem>
        <NButton type="primary" block :loading="loading" @click="onSubmit">注 册</NButton>
      </NForm>

      <div class="auth-foot">
        已有账号？<RouterLink to="/login">直接登录</RouterLink>
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
