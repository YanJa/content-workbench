<script setup lang="ts">
/**
 * 用户管理（仅管理员可见）。
 *
 * 三件事：建账号、改角色 / 停用、看全站用量。
 *
 * 设计取舍：**停用而不是删除**。删除账号会让他的历史配图集体 404
 * （存量直链签名绑的是用户 id，写死在正文里了），而且不可逆。停用
 * 立刻收回门禁、随时能恢复，数据原样留着。
 *
 * 「不能对自己动手」「不能清空最后一个管理员」这两条后端会拦（400），
 * 这里就在按钮上先禁用 + 给 tooltip 说明，别让人点了才知道不行。
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NNumberAnimation,
  NSelect,
  NSpin,
  NStatistic,
  NSwitch,
  NTag,
  NText,
  useDialog,
  useMessage,
  type DataTableColumns,
  type SelectOption,
} from 'naive-ui'

import {
  createUser,
  getAdminStats,
  listUsers,
  updateUser,
  type AdminStats,
  type AdminUser,
} from '@/api/admin'
import type { Role } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const message = useMessage()
const dialog = useDialog()
const auth = useAuthStore()

const users = ref<AdminUser[]>([])
const stats = ref<AdminStats | null>(null)
const loading = ref(true)
const err = ref('')

// 建账号弹窗
const showCreate = ref(false)
const creating = ref(false)
const form = ref({ username: '', password: '', role: 'member' as Role })

const ROLE_OPTIONS: SelectOption[] = [
  { label: '管理员', value: 'admin' },
  { label: '普通成员', value: 'member' },
]

const myId = computed(() => auth.user?.id ?? '')
/** 当前活跃管理员数：只剩一个时不许把他的角色降掉 */
const activeAdminCount = computed(
  () => users.value.filter((u) => u.role === 'admin').length,
)

async function load() {
  loading.value = true
  err.value = ''
  try {
    users.value = await listUsers()
    stats.value = await getAdminStats()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function roleTag(role: Role) {
  return h(
    NTag,
    { size: 'tiny', bordered: false, type: role === 'admin' ? 'warning' : 'default' },
    { default: () => (role === 'admin' ? '管理员' : '成员') },
  )
}

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function onCreate() {
  if (!form.value.username.trim()) {
    message.warning('请填用户名')
    return
  }
  if (form.value.password.length < 8) {
    message.warning('密码至少 8 位')
    return
  }
  creating.value = true
  try {
    const u = await createUser({
      username: form.value.username.trim(),
      password: form.value.password,
      role: form.value.role,
    })
    message.success(`已创建账号 ${u.username}`)
    showCreate.value = false
    form.value = { username: '', password: '', role: 'member' }
    await load()
  } catch (e) {
    message.error(`创建失败：${(e as Error).message}`)
  } finally {
    creating.value = false
  }
}

async function onChangeRole(row: AdminUser, role: Role) {
  try {
    await updateUser(row.id, { role })
    message.success(`已把 ${row.username} 设为${role === 'admin' ? '管理员' : '普通成员'}`)
    await load()
  } catch (e) {
    message.error(`修改失败：${(e as Error).message}`)
    await load()
  }
}

function onToggleActive(row: AdminUser, next: boolean) {
  const verb = next ? '恢复启用' : '停用'
  dialog.warning({
    title: `${verb}账号`,
    content: next
      ? `恢复后 ${row.username} 可以重新登录，他的数据原样保留。`
      : `停用后 ${row.username} 立刻无法登录（不用等令牌过期），数据保留、随时可恢复。`,
    positiveText: verb,
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await updateUser(row.id, { is_active: next })
        message.success(`已${verb} ${row.username}`)
        await load()
      } catch (e) {
        message.error(`操作失败：${(e as Error).message}`)
        await load()
      }
    },
  })
}

const columns = computed<DataTableColumns<AdminUser>>(() => [
  {
    title: '用户名',
    key: 'username',
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:6px' }, [
        row.username,
        row.id === myId.value
          ? h(NTag, { size: 'tiny', bordered: false, type: 'info' }, { default: () => '我' })
          : null,
        row.is_active ? null : h(NTag, { size: 'tiny', bordered: false }, { default: () => '已停用' }),
      ]),
  },
  {
    title: '角色',
    key: 'role',
    width: 150,
    // 自己的角色不能改（后端 400），只剩一个管理员时也不许降级
    render: (row) =>
      row.id === myId.value
        ? roleTag(row.role)
        : h(NSelect, {
            size: 'small',
            value: row.role,
            options: ROLE_OPTIONS,
            disabled: row.role === 'admin' && activeAdminCount.value <= 1,
            onUpdateValue: (v: Role) => onChangeRole(row, v),
          }),
  },
  { title: '内容数', key: 'contents', width: 90 },
  {
    title: '注册时间',
    key: 'created_at',
    width: 150,
    render: (row) => h(NText, { depth: 3 }, { default: () => fmtTime(row.created_at) }),
  },
  {
    title: '最近登录',
    key: 'last_login_at',
    width: 150,
    render: (row) => h(NText, { depth: 3 }, { default: () => fmtTime(row.last_login_at) }),
  },
  {
    title: '状态',
    key: 'is_active',
    width: 90,
    render: (row) =>
      h(NSwitch, {
        size: 'small',
        value: row.is_active,
        disabled: row.id === myId.value,
        onUpdateValue: (v: boolean) => onToggleActive(row, v),
      }),
  },
])

const usageColumns: DataTableColumns<AdminStats['by_user'][number]> = [
  { title: '用户', key: 'username', width: 140 },
  { title: '内容', key: 'contents', width: 80 },
  { title: '调用次数', key: 'llm_calls', width: 100 },
  { title: 'Token', key: 'total_tokens', width: 120 },
  {
    title: '花费',
    key: 'cost',
    width: 120,
    render: (row) => `¥${row.cost.toFixed(4)}`,
  },
]

onMounted(load)
</script>

<template>
  <div class="admin-users">
    <NAlert v-if="err" type="error" :show-icon="false" style="margin-bottom: 16px">
      {{ err }}
    </NAlert>

    <div v-if="stats" class="stat-row">
      <NCard size="small" class="stat-card">
        <NStatistic label="用户总数">
          <NNumberAnimation :from="0" :to="stats.users.total" />
        </NStatistic>
      </NCard>
      <NCard size="small" class="stat-card">
        <NStatistic label="启用中">
          <NNumberAnimation :from="0" :to="stats.users.active" />
        </NStatistic>
      </NCard>
      <NCard size="small" class="stat-card">
        <NStatistic label="内容总数">
          <NNumberAnimation :from="0" :to="stats.contents" />
        </NStatistic>
      </NCard>
      <NCard size="small" class="stat-card">
        <NStatistic label="累计花费">
          <span class="cost">¥{{ stats.llm.cost.toFixed(4) }}</span>
        </NStatistic>
      </NCard>
    </div>

    <NCard size="small" title="账号" style="margin-bottom: 16px">
      <template #header-extra>
        <NButton size="small" type="primary" @click="showCreate = true">新建账号</NButton>
      </template>
      <NSpin :show="loading">
        <NDataTable :columns="columns" :data="users" :bordered="false" size="small" />
      </NSpin>
      <div class="hint">
        停用即时生效：被停用的人手里那张还没过期的令牌下一次请求就会失效。
        停用不删数据，随时可以再打开。
      </div>
    </NCard>

    <NCard v-if="stats" size="small" title="各账号用量">
      <NDataTable
        :columns="usageColumns"
        :data="stats.by_user"
        :bordered="false"
        size="small"
      />
    </NCard>

    <NModal v-model:show="showCreate" preset="card" title="新建账号" style="width: 420px">
      <NForm label-placement="top">
        <NFormItem label="用户名">
          <NInput v-model:value="form.username" placeholder="字母、数字、_ 或 -，3~32 位" />
        </NFormItem>
        <NFormItem label="初始密码">
          <NInput
            v-model:value="form.password"
            type="password"
            show-password-on="click"
            placeholder="至少 8 位"
          />
        </NFormItem>
        <NFormItem label="角色">
          <NSelect v-model:value="form.role" :options="ROLE_OPTIONS" />
        </NFormItem>
      </NForm>
      <template #footer>
        <div class="modal-foot">
          <NButton size="small" @click="showCreate = false">取消</NButton>
          <NButton size="small" type="primary" :loading="creating" @click="onCreate">
            创建
          </NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.stat-card {
  text-align: center;
}

.cost {
  font-variant-numeric: tabular-nums;
}

.hint {
  margin-top: 10px;
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.6;
}

.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
