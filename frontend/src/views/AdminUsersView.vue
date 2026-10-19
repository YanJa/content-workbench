<script setup lang="ts">
/**
 * 用户管理（仅管理员可见）。
 *
 * 四件事：建账号、改角色 / 停用、看全站用量、**配权限与配额**。
 *
 * 设计取舍：**停用而不是删除**。删除账号会让他的历史配图集体 404
 * （存量直链签名绑的是用户 id，写死在正文里了），而且不可逆。停用
 * 立刻收回门禁、随时能恢复，数据原样留着。
 *
 * 「不能对自己动手」「不能清空最后一个管理员」这两条后端会拦（400），
 * 这里就在按钮上先禁用 + 给 tooltip 说明，别让人点了才知道不行。
 *
 * 权限与配额（按钮级，2026-10 上线）
 * ---------------------------------
 * 两层配置，界面也跟着分两层：
 *
 * 1. **角色模板**（member）：勾选矩阵 + 每个功能的次数上限。改一次影响所有
 *    没被单独覆盖的 member。
 * 2. **个人覆盖**：在「有效权限」上直接勾。保存时按角色模板反算差集
 *    （grant = 我勾的 − 模板，deny = 模板 − 我勾的），所以这是个**增量**
 *    覆盖而不是快照 —— 模板日后加新功能，这个人照样能收到。
 *
 * admin 那一层刻意只给配额不给权限勾选：管理员权限固定全开，接口层直接
 * 400。但配额照配 —— 那是成本闸门，不是权限。
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NCheckboxGroup,
  NDataTable,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NNumberAnimation,
  NSelect,
  NSpin,
  NStatistic,
  NSwitch,
  NTag,
  NText,
  NTooltip,
  useDialog,
  useMessage,
  type DataTableColumns,
  type SelectOption,
} from 'naive-ui'

import {
  createUser,
  createInviteCode,
  deleteInviteCode,
  deleteUser,
  disableInviteCode,
  enableInviteCode,
  getAdminStats,
  getPermissionCatalog,
  listInviteCodes,
  listUsers,
  putRolePermissions,
  putUserOverrides,
  resetUserPassword,
  resetUserQuota,
  updateInviteCode,
  updateUser,
  type AdminStats,
  type AdminUser,
  type InviteCodeItem,
  type PermissionCatalog,
} from '@/api/admin'
import type { QuotaConf, QuotaPeriod, Role } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'
import { actionBtn } from '@/utils/actionBtn'
import IconTextBtn from '@/components/IconTextBtn.vue'

const message = useMessage()
const dialog = useDialog()
const auth = useAuthStore()
const router = useRouter()

// 内容总数卡片 → 跳转到发布队列（内容成品集中管理页）
function goQueue() {
  void router.push('/queue')
}

const users = ref<AdminUser[]>([])
const stats = ref<AdminStats | null>(null)
const catalog = ref<PermissionCatalog | null>(null)
const loading = ref(true)
const err = ref('')

// 邀请码管理
const inviteCodes = ref<InviteCodeItem[]>([])
const inviteLoading = ref(false)
const inviteDraft = ref({ code: '', note: '', maxUses: null as number | null })
const creatingInvite = ref(false)
const inviteBusy = ref('') // 正在停用/启用/删除的码 id

/** 新建账号弹窗里的「来源邀请码」选项：码 + 渠道名（有备注就带上，方便辨认） */
const inviteSourceOptions = computed<SelectOption[]>(() =>
  inviteCodes.value.map((c) => ({
    label: c.note ? `${c.code}（${c.note}）` : c.code,
    value: c.code,
  })),
)

// 建账号弹窗
const showCreate = ref(false)
const creating = ref(false)
const form = ref({ username: '', password: '', role: 'member' as Role, invite_code: '' })

// 卡片折叠状态（卡片越来越多，默认折叠次要卡片，只展开「账号」主卡）
const collapsedCards = ref<Record<string, boolean>>({
  accounts: false, // 账号（主卡，默认展开）
  memberTemplate: true, // 权限模板 · 普通成员
  adminQuota: true, // 配额 · 管理员
  invite: false, // 邀请码管理
})

/** 折叠开关的切换；用一个响应式对象让模板里 v-model 直接绑到字段上 */
function toggleCard(key: string) {
  collapsedCards.value[key] = !collapsedCards.value[key]
}

// 累计花费详情弹框（点「详情」看每个账号的用量明细）
const showUsageDetail = ref(false)
const usageDetailColumns: DataTableColumns<AdminStats['by_user'][number]> = [
  { title: '用户', key: 'username', width: 140 },
  { title: '角色', key: 'role', width: 90, render: (row) => (row.role === 'admin' ? '管理员' : '成员') },
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

// 重置密码弹窗
const showResetPwd = ref(false)
const resettingPwd = ref(false)
const resetPwdTarget = ref<AdminUser | null>(null)
const resetPwdValue = ref('')

// 渠道名编辑：正在编辑的行 + 草稿值
const editingNoteId = ref('')
const editingNoteValue = ref('')

// 角色模板编辑
const savingTemplate = ref(false)
const memberPerms = ref<string[]>([])
const memberQuotas = ref<Record<string, QuotaDraft>>({})
const adminQuotas = ref<Record<string, QuotaDraft>>({})

// 个人覆盖弹窗
const showEdit = ref(false)
const editing = ref<AdminUser | null>(null)
const editPerms = ref<string[]>([])
const editQuotas = ref<Record<string, QuotaDraft>>({})
const savingEdit = ref(false)
const resetting = ref(false)

/** 弹窗里正在编辑的那一行（showEdit 为 false 时也是 null，模板里少写一层判空） */
const editRow = computed<AdminUser | null>(() => (showEdit.value ? editing.value : null))

/** 配额编辑草稿：period 为 'none' 表示「不限」（后端归一成 limit=null） */
interface QuotaDraft {
  period: QuotaPeriod | 'none'
  limit: number | null
}

const ROLE_OPTIONS: SelectOption[] = [
  { label: '管理员', value: 'admin' },
  { label: '普通成员', value: 'member' },
]

const PERIOD_OPTIONS: SelectOption[] = [
  { label: '不限', value: 'none' },
  { label: '每日', value: 'day' },
  { label: '每月', value: 'month' },
]

/** 权限点的分组显示名。后端只给 group key，展示名放前端（它本来就只服务 UI） */
const GROUP_LABELS: Record<string, string> = {
  menu: '菜单可见性',
  chat: '对话',
  queue: '发布队列',
  video: '视频',
  layout: '排版',
  voice: '声音',
  wechat: '公众号',
  settings: '系统',
}

const myId = computed(() => auth.user?.id ?? '')
/** 当前活跃管理员数：只剩一个时不许把他的角色降掉 */
const activeAdminCount = computed(
  () => users.value.filter((u) => u.role === 'admin').length,
)

/** 非菜单的功能权限点 —— 勾选矩阵分两块展示，菜单单独一块并标注「控制侧栏可见性」 */
const functionItems = computed(
  () => (catalog.value?.permissions ?? []).filter((p) => p.group !== 'menu'),
)

const groupedFunctionItems = computed(() => {
  const map = new Map<string, typeof functionItems.value>()
  for (const item of functionItems.value) {
    if (!map.has(item.group)) map.set(item.group, [])
    map.get(item.group)!.push(item)
  }
  return [...map.entries()]
})

const quotaFeatures = computed(() => catalog.value?.quota_features ?? [])

// ---------------------------------------------------------------- 加载
async function load() {
  loading.value = true
  err.value = ''
  try {
    const [list, s, c] = await Promise.all([listUsers(), getAdminStats(), getPermissionCatalog()])
    users.value = list
    stats.value = s
    catalog.value = c
    initTemplateDrafts()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
  void loadInvites()
}

function initTemplateDrafts() {
  const c = catalog.value
  if (!c) return
  const member = c.roles.member
  // 模板没配过时后端返回代码默认值（全部权限点），直接用它当前值初始化
  memberPerms.value = member.permissions ? [...member.permissions] : []
  memberQuotas.value = toDrafts(member.quotas)
  adminQuotas.value = toDrafts(c.roles.admin.quotas)
}

// ---------------------------------------------------------------- 配额草稿换算
function toDrafts(conf: Record<string, QuotaConf> | undefined): Record<string, QuotaDraft> {
  const out: Record<string, QuotaDraft> = {}
  for (const f of quotaFeatures.value) {
    const c = conf?.[f.key]
    out[f.key] = c
      ? { period: c.limit === null ? 'none' : c.period, limit: c.limit }
      : { period: 'none', limit: null }
  }
  return out
}

function fromDrafts(drafts: Record<string, QuotaDraft>): Record<string, QuotaConf> {
  const out: Record<string, QuotaConf> = {}
  for (const [feature, d] of Object.entries(drafts)) {
    out[feature] = d.period === 'none' ? { period: 'day', limit: null } : { period: d.period, limit: d.limit }
  }
  return out
}

/** 角色模板里该功能当前的设定（个人弹窗里提示「继承自角色」用） */
function templateQuotaOf(role: Role, feature: string): QuotaConf | undefined {
  return catalog.value?.roles[role]?.quotas?.[feature]
}

// ---------------------------------------------------------------- 展示辅助
// 图标与图标操作按钮统一走共享实现（utils/icon.ts + components/ActionBtn.vue），
// 保证账号表 / 邀请码表 / 各模块记录操作按钮视觉一致（不再图标文字混用）。
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

function fmtReset(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 配额摘要：只列设了上限的功能，一眼看出谁快用完了 */
function quotaSummary(row: AdminUser): string {
  const parts: string[] = []
  for (const f of quotaFeatures.value) {
    const snap = row.usage?.[f.key]
    if (!snap || snap.unlimited) continue
    const left = snap.left ?? 0
    parts.push(`${f.label} ${snap.used}/${snap.limit}${left === 0 ? ' ⛔' : ''}`)
  }
  return parts.length ? parts.join(' · ') : '均不限'
}

/** 最近的 reset_at：有任何一个功能设了上限就显示，告诉管理员「什么时候自动恢复」 */
function quotaResetHint(row: AdminUser): string {
  let soonest = 0
  for (const f of quotaFeatures.value) {
    const snap = row.usage?.[f.key]
    if (!snap || snap.unlimited) continue
    if (!soonest || snap.reset_at < soonest) soonest = snap.reset_at
  }
  return soonest ? `${fmtReset(soonest)} 重置` : ''
}

// ---------------------------------------------------------------- 账号操作
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
      invite_code: form.value.invite_code.trim() || undefined,
    })
    message.success(`已创建账号 ${u.username}`)
    showCreate.value = false
    form.value = { username: '', password: '', role: 'member', invite_code: '' }
    await load()
  } catch (e) {
    message.error(`创建失败：${(e as Error).message}`)
  } finally {
    creating.value = false
  }
}

/** 打开重置密码弹窗（只对普通用户开放，admin 行不显示该按钮） */
function openResetPwd(row: AdminUser) {
  resetPwdTarget.value = row
  resetPwdValue.value = ''
  showResetPwd.value = true
}

async function onResetPwd() {
  const row = resetPwdTarget.value
  if (!row) return
  if (resetPwdValue.value.length < 8) {
    message.warning('密码至少 8 位')
    return
  }
  resettingPwd.value = true
  try {
    await resetUserPassword(row.id, resetPwdValue.value)
    message.success(`已重置 ${row.username} 的密码`)
    showResetPwd.value = false
    resetPwdTarget.value = null
  } catch (e) {
    message.error(`重置失败：${(e as Error).message}`)
  } finally {
    resettingPwd.value = false
  }
}

/** 删除用户：只对「从没产生过数据」的空账号开放，需二次确认 */
function onDeleteUser(row: AdminUser) {
  dialog.warning({
    title: '删除账号',
    content: `确定删除 ${row.username}？只有从没产生过任何数据（内容、会话、用量、音色等）的空账号才能删除；一旦有数据会被拒绝，请改用「停用」。此操作不可逆。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteUser(row.id)
        message.success(`已删除账号 ${row.username}`)
        await load()
      } catch (e) {
        message.error(`删除失败：${(e as Error).message}`)
        await load()
      }
    },
  })
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

// ---------------------------------------------------------------- 角色模板
async function onSaveMemberTemplate() {
  savingTemplate.value = true
  try {
    await putRolePermissions('member', {
      permissions: memberPerms.value,
      quotas: fromDrafts(memberQuotas.value),
    })
    message.success('已更新成员权限模板')
    await load()
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    savingTemplate.value = false
  }
}

async function onSaveAdminQuota() {
  savingTemplate.value = true
  try {
    await putRolePermissions('admin', { quotas: fromDrafts(adminQuotas.value) })
    message.success('已更新管理员配额')
    await load()
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    savingTemplate.value = false
  }
}

// ---------------------------------------------------------------- 个人覆盖
function openEdit(row: AdminUser) {
  editing.value = row
  showEdit.value = true
  // 勾选矩阵按「有效权限」初始化：管理员看到的就是这个人实际能用的
  editPerms.value = [...row.permissions]
  editQuotas.value = toDrafts(row.quotas)
}

async function onSaveEdit() {
  const row = editRow.value
  if (!row) return
  savingEdit.value = true
  try {
    // 按角色模板反算差集 —— 增量覆盖，不是快照
    const template = new Set(catalog.value?.roles[row.role]?.permissions ?? [])
    const picked = new Set(editPerms.value)
    const grant = [...picked].filter((k) => !template.has(k))
    const deny = [...template].filter((k) => !picked.has(k))

    await putUserOverrides(row.id, {
      perm_override: { grant, deny },
      quota_override: fromDrafts(editQuotas.value),
    })
    message.success(`已更新 ${row.username} 的权限与配额`)
    showEdit.value = false
    await load()
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    savingEdit.value = false
  }
}

function onResetQuota(row: AdminUser) {
  dialog.warning({
    title: '重置用量',
    content: `把 ${row.username} 的本周期计数清零（配置不动，下个周期照样从 0 开始计）。`,
    positiveText: '重置',
    negativeText: '取消',
    onPositiveClick: async () => {
      resetting.value = true
      try {
        await resetUserQuota(row.id)
        message.success(`已重置 ${row.username} 的用量`)
        await load()
      } catch (e) {
        message.error(`重置失败：${(e as Error).message}`)
      } finally {
        resetting.value = false
      }
    },
  })
}

// ---------------------------------------------------------------- 表格
const columns = computed<DataTableColumns<AdminUser>>(() => [
  {
    title: '用户名',
    key: 'username',
    // ⚠️ 必须给 width：fixed 布局下没有 width 的列会瓜分表格剩余空间 ——
    // 其余 8 列全是固定 width，剩余全进了用户名列，截图里它一家独占
    // 200px+（2026-10-19 反馈「web 端用户名列就很宽」）。
    // ⚠️ 也别配 minWidth：naive 会把 minWidth 原样写进 col 的 min-width，
    // 容器窄于列宽总和时 fixed 布局压缩列，min-width 先于 width 触底
    // （1440 视口下 1138 容器，width:150 被 minWidth:140 钳成 140 实测）。
    // minWidth 缺省时 naive 用 width 兜底，行为一致。
    // 150 够放「admin + 我」，长用户名 ellipsis 兜底。
    width: 150,
    // 移动端列会变窄：用 ellipsis 截断 + 原生 title 显示全名，避免长用户名换行撑高整行
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:6px;min-width:0' }, [
        h(
          'span',
          {
            title: row.username,
            // ⚠️ 不能给这个 span 加 flex:1 —— 列宽被 minWidth 撑到 140 时，
            // flex:1 会让 span 盒子占满剩余空间，把后面的「我 / 已停用」tag
            // 推到列右缘：admin 这种短用户名行会出现「admin …30px… 我」的
            // 大间隔（2026-10-19 用户截图反馈）。截断交给 max-width +
            // ellipsis，tag 用 flex-shrink:0 保持紧跟用户名。
            style:
              'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px;min-width:0',
          },
          { default: () => row.username },
        ),
        row.id === myId.value
          ? h(NTag, { size: 'tiny', bordered: false, type: 'info', style: 'flex-shrink:0' }, { default: () => '我' })
          : null,
        row.is_active
          ? null
          : h(NTag, { size: 'tiny', bordered: false, style: 'flex-shrink:0' }, { default: () => '已停用' }),
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
            // 移动端用 tiny：small 的 select 高 28px，加 td padding 把行撑到
            // 45px，窄屏一屏看不了几行（2026-10-19 反馈）。tiny 22px，
            // 行高回落到和邀请码表一致的 38px 左右。桌面仍用 small。
            size: isMobile.value ? 'tiny' : 'small',
            value: row.role,
            options: ROLE_OPTIONS,
            disabled: row.role === 'admin' && activeAdminCount.value <= 1,
            onUpdateValue: (v: Role) => onChangeRole(row, v),
          }),
  },
  { title: '内容数', key: 'contents', width: 90 },
  {
    title: '配额（本周期）',
    key: 'quota',
    width: 260,
    render: (row) => {
      const hint = quotaResetHint(row)
      // 移动端：两行文字（摘要 + 重置时间）压成一行（ellipsis + title 全称），
      // 窄屏横滚表格里两行文字会把行撑到 45-78px，一屏看不了两行用户
      // （2026-10-19 反馈）。重置按钮保留 —— 单行高度内不撑行，且弹窗里
      // 没有替代入口，去掉移动端就没法重置了。
      if (isMobile.value) {
        return h('div', { style: 'display:flex;align-items:center;gap:8px;min-width:0' }, [
          h(
            NText,
            {
              depth: 3,
              title: hint ? `${quotaSummary(row)}（${hint}）` : quotaSummary(row),
              style:
                'font-size:12px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1',
            },
            { default: () => quotaSummary(row) },
          ),
          actionBtn('reset', '重置用量', () => onResetQuota(row), { disabled: resetting.value }),
        ])
      }
      return h('div', { style: 'display:flex;align-items:center;gap:8px' }, [
        h('div', { style: 'flex:1;min-width:0' }, [
          h(
            NText,
            { depth: 3, style: 'font-size:12px;line-height:1.5' },
            { default: () => quotaSummary(row) },
          ),
          hint
            ? h(
                NText,
                { depth: 3, style: 'font-size:11px;line-height:1.5;display:block' },
                { default: () => hint },
              )
            : null,
        ]),
        actionBtn('reset', '重置用量', () => onResetQuota(row), { disabled: resetting.value }),
      ])
    },
  },
  {
    title: '操作',
    key: 'perm',
    width: 120,
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:2px' }, [
        actionBtn('gear', row.role === 'admin' ? '配置配额' : '配置权限与配额', () => openEdit(row)),
        // 重置密码：只对普通用户开放（管理员账号的密码不走重置旁路）
        row.role === 'member' ? actionBtn('key', '重置密码', () => openResetPwd(row)) : null,
        // 删除：仅非自己、非管理员（空账号才能删，后端会拦有数据的）
        row.id !== myId.value && row.role !== 'admin'
          ? actionBtn('trash', '删除（仅限空账号）', () => onDeleteUser(row), { type: 'error' })
          : null,
      ]),
  },
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
    title: '来源',
    key: 'invite',
    width: 140,
    render: (row) =>
      row.invite_note
        ? h(NText, { depth: 3 }, { default: () => row.invite_note })
        : h(NText, { depth: 3 }, { default: () => '—' }),
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

// 「各账号用量」已合并进「累计花费 → 详情」弹框（usageDetailColumns 见上方），
// 不再单独占一张卡片，页面更清爽。

// ---------------------------------------------------------------- 邀请码管理
async function loadInvites() {
  inviteLoading.value = true
  try {
    inviteCodes.value = await listInviteCodes()
  } catch (e) {
    message.error(`加载邀请码失败：${(e as Error).message}`)
  } finally {
    inviteLoading.value = false
  }
}

async function onCreateInvite() {
  const code = inviteDraft.value.code.trim()
  if (code && (code.length < 3 || /\s/.test(code))) {
    message.warning('自定义码需为 3 位以上且不含空格')
    return
  }
  creatingInvite.value = true
  try {
    await createInviteCode({
      code: code || undefined,
      note: inviteDraft.value.note.trim(),
      max_uses: inviteDraft.value.maxUses,
    })
    message.success('已生成邀请码')
    inviteDraft.value = { code: '', note: '', maxUses: null }
    await loadInvites()
  } catch (e) {
    message.error(`生成失败：${(e as Error).message}`)
  } finally {
    creatingInvite.value = false
  }
}

async function onToggleInvite(item: InviteCodeItem) {
  inviteBusy.value = item.id
  try {
    if (item.disabled) {
      await enableInviteCode(item.id)
      message.success('已启用')
    } else {
      await disableInviteCode(item.id)
      message.success('已停用，该码立即不能再注册')
    }
    await loadInvites()
  } catch (e) {
    message.error(`操作失败：${(e as Error).message}`)
  } finally {
    inviteBusy.value = ''
  }
}

async function onDeleteInvite(item: InviteCodeItem) {
  inviteBusy.value = item.id
  try {
    await deleteInviteCode(item.id)
    message.success('已删除')
    await loadInvites()
  } catch (e) {
    message.error(`删除失败：${(e as Error).message}`)
  } finally {
    inviteBusy.value = ''
  }
}

async function onCopyInvite(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    message.success('已复制到剪贴板')
  } catch {
    // 剪贴板 API 在非 https / 权限被拒时会失败，退回提示手动复制
    message.info(`请手动复制：${code}`)
  }
}

/** 开始编辑某条邀请码的渠道名 */
function startEditNote(item: InviteCodeItem) {
  editingNoteId.value = item.id
  editingNoteValue.value = item.note
}

/** 取消编辑渠道名 */
function cancelEditNote() {
  editingNoteId.value = ''
  editingNoteValue.value = ''
}

/** 保存渠道名 */
async function saveNote(item: InviteCodeItem) {
  const val = editingNoteValue.value.trim()
  try {
    await updateInviteCode(item.id, val)
    message.success('渠道名已更新')
    cancelEditNote()
    await loadInvites()
    // 渠道名变了，用户列表的「来源」列也要跟着刷新
    await load()
  } catch (e) {
    message.error(`更新失败：${(e as Error).message}`)
  }
}

/** 邀请码列表的用量文案：不限 / 3/10 / 已用完 */
function inviteUsageText(item: InviteCodeItem): string {
  if (item.max_uses === null) return `已用 ${item.used} · 不限`
  if (item.exhausted) return `已用 ${item.used}/${item.max_uses} · 已用完`
  return `已用 ${item.used}/${item.max_uses}`
}

const inviteColumns: DataTableColumns<InviteCodeItem> = [
  {
    title: '邀请码',
    key: 'code',
    width: 200,
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:6px;min-width:0' }, [
        h(
          NText,
          {
            code: true,
            title: row.code,
            style:
              'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0',
          },
          { default: () => row.code },
        ),
        actionBtn('copy', '复制邀请码', () => onCopyInvite(row.code)),
      ]),
  },
  {
    title: '渠道',
    key: 'note',
    width: 220,
    render: (row) => {
      // 正在编辑这行 → 输入框 + 保存/取消图标
      if (editingNoteId.value === row.id) {
        return h('div', { style: 'display:flex;align-items:center;gap:6px' }, [
          h(NInput, {
            size: 'small',
            value: editingNoteValue.value,
            placeholder: '渠道备注',
            style: 'flex:1;min-width:0',
            onUpdateValue: (v: string) => (editingNoteValue.value = v),
          }),
          actionBtn('check', '保存', () => saveNote(row), { type: 'primary' }),
          actionBtn('close', '取消', cancelEditNote),
        ])
      }
      // 非编辑态：渠道名 + 铅笔图标（点一下进入编辑）
      return h('div', { style: 'display:flex;align-items:center;gap:6px' }, [
        h(
          NText,
          { depth: row.note ? 1 : 3, style: 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' },
          { default: () => row.note || '未备注' },
        ),
        actionBtn('edit', '修改渠道名', () => startEditNote(row)),
      ])
    },
  },
  {
    title: '用量',
    key: 'usage',
    width: 150,
    render: (row) =>
      h(NText, { type: row.exhausted ? 'error' : undefined }, { default: () => inviteUsageText(row) }),
  },
  {
    title: '状态',
    key: 'disabled',
    width: 100,
    render: (row) =>
      row.exhausted
        ? h(NTag, { size: 'tiny', type: 'warning', bordered: false }, { default: () => '已用完' })
        : row.disabled
          ? h(NTag, { size: 'tiny', type: 'error', bordered: false }, { default: () => '已停用' })
          : h(NTag, { size: 'tiny', type: 'success', bordered: false }, { default: () => '生效中' }),
  },
  {
    title: '操作',
    key: 'op',
    width: 90,
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:2px' }, [
        actionBtn('power', row.disabled ? '启用' : '停用', () => onToggleInvite(row), {
          loading: inviteBusy.value === row.id,
          disabled: row.exhausted,
        }),
        // 用过的码不能删（渠道统计要保留），只有 used === 0 的码显示删除
        row.used === 0
          ? actionBtn('trash', '删除', () => onDeleteInvite(row), {
              type: 'error',
              loading: inviteBusy.value === row.id,
            })
          : null,
      ]),
  },
]

// 移动端断点与 AppShell 保持一致（768px）：账号表格在窄屏下走「单行摘要」
// 渲染，避免两行配额文字把行撑高。初始值在 script 顶层就算好 —— 首渲染
// 早于 onMounted，等挂载后再赋值会让移动端先闪一帧桌面布局。
const isMobile = ref(
  typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches,
)

onMounted(async () => {
  const mq = window.matchMedia('(max-width: 767px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', (e) => {
    isMobile.value = e.matches
  })
  await load()
})
</script>

<template>
  <div class="admin-users">
    <NAlert v-if="err" type="error" :show-icon="false" style="margin-bottom: 16px">
      {{ err }}
    </NAlert>

    <div v-if="stats" class="stat-row">
      <NCard size="small" class="stat-card">
        <NStatistic label="用户">
          <div class="stat-user">
            <NNumberAnimation :from="0" :to="stats.users.total" />
            <span class="stat-user-sub">启用 {{ stats.users.active }}</span>
          </div>
        </NStatistic>
      </NCard>
      <NCard size="small" class="stat-card">
        <NStatistic label="内容总数">
          <div class="stat-link-inner">
            <NNumberAnimation :from="0" :to="stats.contents" />
          </div>
        </NStatistic>
        <div class="stat-link-hint">AI 产出的全部内容</div>
      </NCard>
      <NCard size="small" class="stat-card stat-card-link">
        <div
          class="stat-link"
          role="button"
          tabindex="0"
          @click="goQueue"
          @keydown.enter="goQueue"
        >
          <NStatistic label="队列中">
            <div class="stat-link-inner">
              <NNumberAnimation :from="0" :to="stats.queue_items" />
              <svg class="stat-link-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </div>
          </NStatistic>
          <div class="stat-link-hint">前往发布队列</div>
        </div>
      </NCard>
      <NCard size="small" class="stat-card">
        <NStatistic label="累计花费">
          <span class="cost">¥{{ stats.llm.cost.toFixed(4) }}</span>
        </NStatistic>
        <NTooltip trigger="hover">
          <template #trigger>
            <NButton
              size="tiny"
              quaternary
              circle
              class="stat-detail-btn"
              @click="showUsageDetail = true"
            >
              <template #icon>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="3" y1="6" x2="15" y2="6" />
                  <line x1="3" y1="12" x2="15" y2="12" />
                  <line x1="3" y1="18" x2="11" y2="18" />
                  <polyline points="15 15 19 19 23 15" />
                </svg>
              </template>
            </NButton>
          </template>
          查看各账号用量明细
        </NTooltip>
      </NCard>
    </div>

    <NCard size="small" title="账号" style="margin-bottom: 16px">
      <template #header-extra>
        <div class="card-extra">
          <NTooltip trigger="hover">
            <template #trigger>
              <NButton
                size="tiny"
                quaternary
                circle
                @click="toggleCard('accounts')"
              >
                <template #icon>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <polyline v-if="collapsedCards.accounts" points="6 9 12 15 18 9" />
                    <polyline v-else points="18 15 12 9 6 15" />
                  </svg>
                </template>
              </NButton>
            </template>
            {{ collapsedCards.accounts ? '展开' : '折叠' }}
          </NTooltip>
          <IconTextBtn name="plus" type="primary" @click="showCreate = true">新建账号</IconTextBtn>
        </div>
      </template>
      <div v-show="!collapsedCards.accounts">
        <NSpin :show="loading">
          <NDataTable
            :columns="columns"
            :data="users"
            :bordered="false"
            size="small"
            :scroll-x="1240"
          />
        </NSpin>
        <div class="hint">
          停用即时生效：被停用的人手里那张还没过期的令牌下一次请求就会失效。
          停用不删数据，随时可以再打开。
        </div>
      </div>
    </NCard>

    <!-- 角色模板：member 的权限清单 + 各功能次数上限 -->
    <NCard v-if="catalog" size="small" title="权限模板 · 普通成员" style="margin-bottom: 16px">
      <template #header-extra>
        <NTooltip trigger="hover">
          <template #trigger>
            <NButton
              size="tiny"
              quaternary
              circle
              @click="toggleCard('memberTemplate')"
            >
              <template #icon>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <polyline v-if="collapsedCards.memberTemplate" points="6 9 12 15 18 9" />
                  <polyline v-else points="18 15 12 9 6 15" />
                </svg>
              </template>
            </NButton>
          </template>
          {{ collapsedCards.memberTemplate ? '展开' : '折叠' }}
        </NTooltip>
      </template>
      <NSpin v-show="!collapsedCards.memberTemplate" :show="loading">
        <div class="perm-grid">
          <div class="perm-group">
            <div class="perm-group-title">菜单可见性</div>
            <NCheckboxGroup v-model:value="memberPerms" class="perm-checks">
              <NCheckbox
                v-for="item in (catalog.permissions.filter((p) => p.group === 'menu'))"
                :key="item.key"
                :value="item.key"
                :label="item.label"
              />
            </NCheckboxGroup>
            <div class="group-hint">取消勾选后，对应菜单从侧栏消失，直接敲网址也会被弹走</div>
          </div>
          <div v-for="[group, items] in groupedFunctionItems" :key="group" class="perm-group">
            <div class="perm-group-title">{{ GROUP_LABELS[group] ?? group }}</div>
            <NCheckboxGroup v-model:value="memberPerms" class="perm-checks">
              <NCheckbox
                v-for="item in items"
                :key="item.key"
                :value="item.key"
                :label="item.label"
              />
            </NCheckboxGroup>
          </div>
        </div>

        <div class="quota-block">
          <div class="quota-title">
            使用次数配额
            <NText depth="3" style="font-weight: normal; margin-left: 8px">
              提交即预扣，失败不退还（异步任务资源已经花掉了）；推送草稿是唯一同步可判成败的，失败会退还
            </NText>
          </div>
          <div class="quota-row">
            <template v-for="f in quotaFeatures" :key="f.key">
              <div class="quota-item">
                <span class="quota-label">{{ f.label }}</span>
                <NSelect
                  v-model:value="memberQuotas[f.key].period"
                  :options="PERIOD_OPTIONS"
                  size="small"
                  style="width: 84px"
                />
                <NInputNumber
                  v-if="memberQuotas[f.key].period !== 'none'"
                  v-model:value="memberQuotas[f.key].limit"
                  :min="1"
                  :max="99999"
                  size="small"
                  style="width: 96px"
                  placeholder="次数"
                />
                <span v-else class="quota-unlimited">不限</span>
              </div>
            </template>
          </div>
        </div>

        <div class="card-foot">
          <IconTextBtn name="check" type="primary" :loading="savingTemplate" @click="onSaveMemberTemplate">
            保存模板
          </IconTextBtn>
          <span class="foot-hint">
            改这里会影响所有没有被单独覆盖过的成员；被单独覆盖的人按个人配置走。
          </span>
        </div>
      </NSpin>
    </NCard>

    <!-- 管理员：只给配额，权限固定全开 -->
    <NCard v-if="catalog" size="small" title="配额 · 管理员" style="margin-bottom: 16px">
      <template #header-extra>
        <NTooltip trigger="hover">
          <template #trigger>
            <NButton
              size="tiny"
              quaternary
              circle
              @click="toggleCard('adminQuota')"
            >
              <template #icon>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <polyline v-if="collapsedCards.adminQuota" points="6 9 12 15 18 9" />
                  <polyline v-else points="18 15 12 9 6 15" />
                </svg>
              </template>
            </NButton>
          </template>
          {{ collapsedCards.adminQuota ? '展开' : '折叠' }}
        </NTooltip>
      </template>
      <div v-show="!collapsedCards.adminQuota">
        <NAlert type="info" :show-icon="false" style="margin-bottom: 12px">
          管理员权限固定为全部开放、不可收窄（防止把自己锁在门外）。但配额照走 ——
          那是成本闸门，不是权限。
        </NAlert>
        <div class="quota-row">
          <template v-for="f in quotaFeatures" :key="f.key">
            <div class="quota-item">
              <span class="quota-label">{{ f.label }}</span>
              <NSelect
                v-model:value="adminQuotas[f.key].period"
                :options="PERIOD_OPTIONS"
                size="small"
                style="width: 84px"
              />
              <NInputNumber
                v-if="adminQuotas[f.key].period !== 'none'"
                v-model:value="adminQuotas[f.key].limit"
                :min="1"
                :max="99999"
                size="small"
                style="width: 96px"
                placeholder="次数"
              />
              <span v-else class="quota-unlimited">不限</span>
            </div>
          </template>
        </div>
        <div class="card-foot">
          <IconTextBtn name="check" type="primary" :loading="savingTemplate" @click="onSaveAdminQuota">
            保存配额
          </IconTextBtn>
        </div>
      </div>
    </NCard>

    <!-- 邀请码管理：生成限次/不限次的码，复制发给用户注册，渠道统计 -->
    <NCard size="small" title="邀请码管理" style="margin-bottom: 16px">
      <template #header-extra>
        <div class="card-extra">
          <NTooltip trigger="hover">
            <template #trigger>
              <NButton
                size="tiny"
                quaternary
                circle
                @click="toggleCard('invite')"
              >
                <template #icon>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                    <polyline v-if="collapsedCards.invite" points="6 9 12 15 18 9" />
                    <polyline v-else points="18 15 12 9 6 15" />
                  </svg>
                </template>
              </NButton>
            </template>
            {{ collapsedCards.invite ? '展开' : '折叠' }}
          </NTooltip>
          <NText depth="3" style="font-size: 12px">注册码 + 渠道统计</NText>
        </div>
      </template>
      <div v-show="!collapsedCards.invite">
        <div class="invite-create">
          <NInput
            v-model:value="inviteDraft.code"
            placeholder="自定义码（留空随机生成）"
            style="width: 200px"
          />
          <NInput
            v-model:value="inviteDraft.note"
            placeholder="渠道备注，如「小红书投放」"
            style="width: 220px"
          />
          <NInputNumber
            v-model:value="inviteDraft.maxUses"
            :min="1"
            :show-button="false"
            placeholder="次数上限（空=不限）"
            style="width: 160px"
          />
          <IconTextBtn name="plus" type="primary" :loading="creatingInvite" @click="onCreateInvite">
            生成邀请码
          </IconTextBtn>
        </div>
        <NSpin :show="inviteLoading" style="margin-top: 12px">
          <NDataTable
            :columns="inviteColumns"
            :data="inviteCodes"
            :bordered="false"
            size="small"
            :scroll-x="900"
          />
        </NSpin>
        <div class="hint">
          一个码可限制注册 N 个账号；用尽后自动失效但保留记录，方便对账。
          用过的码不能删除（它是用户「来源」的依据），只能停用。
        </div>
      </div>
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
        <NFormItem label="来源邀请码（可选）">
          <NSelect
            v-model:value="form.invite_code"
            :options="inviteSourceOptions"
            clearable
            filterable
            placeholder="选择渠道（空 = 无来源）"
          />
        </NFormItem>
      </NForm>
      <template #footer>
        <div class="modal-foot">
          <NButton size="small" @click="showCreate = false">取消</NButton>
          <IconTextBtn name="plus" type="primary" :loading="creating" @click="onCreate">
            创建
          </IconTextBtn>
        </div>
      </template>
    </NModal>

    <!-- 个人权限/配额覆盖 -->
    <NModal
      v-model:show="showEdit"
      preset="card"
      :title="`权限与配额 · ${editing?.username ?? ''}`"
      style="width: 720px"
    >
      <NAlert v-if="editRow?.role === 'admin'" type="info" :show-icon="false" style="margin-bottom: 12px">
        管理员权限固定全开，这里只能调他的配额。
      </NAlert>
      <NAlert v-else type="info" :show-icon="false" style="margin-bottom: 12px">
        按「实际能用什么」勾选即可，保存时自动按成员模板反算差异 ——
        这是增量覆盖，模板以后新增的功能他照样能收到。
      </NAlert>

      <div class="perm-grid">
        <div class="perm-group">
          <div class="perm-group-title">菜单可见性</div>
          <NCheckboxGroup v-model:value="editPerms" class="perm-checks">
            <NCheckbox
              v-for="item in (catalog?.permissions.filter((p) => p.group === 'menu') ?? [])"
              :key="item.key"
              :value="item.key"
              :label="item.label"
              :disabled="editRow?.role === 'admin'"
            />
          </NCheckboxGroup>
        </div>
        <div v-for="[group, items] in groupedFunctionItems" :key="group" class="perm-group">
          <div class="perm-group-title">{{ GROUP_LABELS[group] ?? group }}</div>
          <NCheckboxGroup v-model:value="editPerms" class="perm-checks">
            <NCheckbox
              v-for="item in items"
              :key="item.key"
              :value="item.key"
              :label="item.label"
              :disabled="editRow?.role === 'admin'"
            />
          </NCheckboxGroup>
        </div>
      </div>

      <div class="quota-block">
        <div class="quota-title">个人配额覆盖</div>
        <div class="quota-row">
          <template v-for="f in quotaFeatures" :key="f.key">
            <div class="quota-item">
              <span class="quota-label">{{ f.label }}</span>
              <NSelect
                v-model:value="editQuotas[f.key].period"
                :options="PERIOD_OPTIONS"
                size="small"
                style="width: 84px"
              />
              <NInputNumber
                v-if="editQuotas[f.key].period !== 'none'"
                v-model:value="editQuotas[f.key].limit"
                :min="1"
                :max="99999"
                size="small"
                style="width: 96px"
                placeholder="次数"
              />
              <span v-else class="quota-unlimited">
                不限{{
                  templateQuotaOf(editRow?.role ?? 'member', f.key)?.limit != null
                    ? `（模板 ${templateQuotaOf(editRow?.role ?? 'member', f.key)?.limit}）`
                    : ''
                }}
              </span>
            </div>
          </template>
        </div>
      </div>

      <template #footer>
        <div class="modal-foot">
          <NButton size="small" :disabled="savingEdit" @click="showEdit = false">取消</NButton>
          <IconTextBtn name="check" type="primary" :loading="savingEdit" @click="onSaveEdit">
            保存
          </IconTextBtn>
        </div>
      </template>
    </NModal>

    <!-- 重置普通用户密码 -->
    <NModal
      v-model:show="showResetPwd"
      preset="card"
      :title="`重置密码 · ${resetPwdTarget?.username ?? ''}`"
      style="width: 420px"
    >
      <NForm label-placement="top">
        <NFormItem label="新密码">
          <NInput
            v-model:value="resetPwdValue"
            type="password"
            show-password-on="click"
            placeholder="至少 8 位"
          />
        </NFormItem>
      </NForm>
      <NAlert type="info" :show-icon="false" style="margin-top: 4px">
        重置后对方需用新密码重新登录。管理员账号的密码不能在这里重置。
      </NAlert>
      <template #footer>
        <div class="modal-foot">
          <NButton size="small" :disabled="resettingPwd" @click="showResetPwd = false">取消</NButton>
          <IconTextBtn name="key" type="primary" :loading="resettingPwd" @click="onResetPwd">
            重置
          </IconTextBtn>
        </div>
      </template>
    </NModal>

    <!-- 累计花费详情：每个账号的用量明细 -->
    <NModal
      v-model:show="showUsageDetail"
      preset="card"
      title="各账号用量明细"
      style="width: 760px"
    >
      <NDataTable
        :columns="usageDetailColumns"
        :data="stats?.by_user ?? []"
        :bordered="false"
        size="small"
        :max-height="480"
      />
      <template #footer>
        <div class="modal-foot">
          <NButton size="small" @click="showUsageDetail = false">关闭</NButton>
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

/* 用户卡片：总数 + 启用数合并 */
.stat-user {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
}

.stat-user-sub {
  font-size: 12px;
  color: var(--n-text-color-3, #999);
  font-variant-numeric: tabular-nums;
}

/* 内容卡片：可点击跳转到发布队列 */
.stat-card-link {
  cursor: pointer;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}

.stat-card-link:hover {
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08);
}

.stat-card-link:active {
  transform: translateY(1px);
}

.stat-link {
  display: flex;
  flex-direction: column;
  align-items: center;
  outline: none;
}

.stat-link-inner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.stat-link-icon {
  color: var(--n-text-color-3, #999);
}

.stat-link-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--n-text-color-3, #999);
}

/* 卡片 header 右侧：折叠按钮 + 主操作按钮并排 */
.card-extra {
  display: flex;
  align-items: center;
  gap: 8px;
}

.cost {
  font-variant-numeric: tabular-nums;
}

/* 累计花费卡里的详情图标按钮：与数字之间留一点间距 */
.stat-detail-btn {
  margin-top: 6px;
}

.hint,
.foot-hint {
  margin-top: 10px;
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.6;
}

.invite-create {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.foot-hint {
  margin-top: 0;
  margin-left: 4px;
}

.perm-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px 20px;
}

.perm-group-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
  color: var(--app-text-2);
}

.group-hint {
  margin-top: 4px;
  font-size: 11px;
  color: var(--app-text-3);
  line-height: 1.5;
}

.perm-checks {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.quota-block {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--app-border, rgba(255, 255, 255, 0.08));
}

.quota-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
  color: var(--app-text-2);
}

.quota-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
}

.quota-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.quota-label {
  font-size: 13px;
  min-width: 68px;
}

.quota-unlimited {
  font-size: 12px;
  color: var(--app-text-3);
}

.card-foot {
  display: flex;
  align-items: center;
  margin-top: 16px;
}

.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
