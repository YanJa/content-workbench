<script setup lang="ts">
/**
 * 系统状态页。
 * 把「服务能不能跑」「AI 配没配」「有哪些工具」「哪些操作要人工确认」
 * 这几件事摊开给运营同学看，出问题能自己定位。
 */
import { computed, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert,
  NBadge,
  NCard,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NDivider,
  NSpin,
  NTag,
  NText,
  NButton,
  NInput,
  NModal,
  NForm,
  NFormItem,
  NSwitch,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'

import { checkHealth } from '@/api/http'
import { listTools } from '@/api/tools'
import { getApiBase, isCustomApiBase, setApiBase } from '@/api/config'
import { useAuthStore } from '@/stores/auth'
import {
  checkWeChatAccount,
  createWeChatAccount,
  deleteWeChatAccount,
  getAuthorInfo,
  listWeChatAccounts,
  setDefaultWeChatAccount,
  updateAuthorInfo,
  updateWeChatAccount,
  uploadWeChatQrcode,
  type WeChatAccountPayload,
} from '@/api/settings'
import type { ToolInfo, WeChatAccount } from '@/types'

interface Health {
  status: string
  env: string
  llm_configured: boolean
  redis: boolean
  tools_enabled: boolean
}

const health = ref<Health | null>(null)
const tools = ref<ToolInfo[]>([])
const loading = ref(true)
const err = ref('')

const model = computed(() => import.meta.env.VITE_MODEL_LABEL || 'deepseek-flash')

const router = useRouter()
const auth = useAuthStore()

// 走 vite dev server 时 /api 已由 proxy 转发到后端，手动指定地址是噪音；
// 只有生产构建（dist 静态部署、没有 dev proxy）时才需要暴露这个配置。
const showApiBase = import.meta.env.PROD

// ---- 后端地址（静态部署或跨域访问时手动指定）----
const apiBaseInput = ref(getApiBase())
const apiBaseSaved = ref(getApiBase())
const apiBaseMsg = ref('')

const usingCustom = computed(() => isCustomApiBase())

// ---- 账号（用户模块：数据归属 = 登录身份，见 backend/app/core/tenant.py）----
const createdAtText = computed(() => {
  const ts = auth.user?.created_at
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
})

interface QuotaRow {
  feature: string
  label: string
  used: number
  limit: number | null
  unlimited: boolean
  left: number | null
  periodLabel: string
  resetText: string
}

/**
 * 我的配额。只列**设了上限**的功能 —— 五个全列出来大半是「不限」，
 * 反而把真正有用的信息淹了。
 */
const quotaRows = computed<QuotaRow[]>(() => {
  const out: QuotaRow[] = []
  for (const [feature, snap] of Object.entries(auth.usage)) {
    if (!snap || snap.unlimited) continue
    out.push({
      feature,
      label: snap.label,
      used: snap.used,
      limit: snap.limit,
      unlimited: false,
      left: snap.left,
      periodLabel: snap.period === 'month' ? '每月' : '每日',
      resetText: fmtReset(snap.reset_at),
    })
  }
  return out
})

function fmtReset(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function onLogout() {
  auth.logout()
  void router.replace('/login')
}

function applyApiBase() {
  const next = setApiBase(apiBaseInput.value)
  apiBaseSaved.value = next
  apiBaseInput.value = next
  apiBaseMsg.value = `已切换到 ${next}`
  // 地址变了要立刻重新探活
  void refresh()
}

function resetApiBase() {
  setApiBase('')
  apiBaseInput.value = getApiBase()
  apiBaseSaved.value = getApiBase()
  apiBaseMsg.value = '已恢复默认地址'
  void refresh()
}

async function refresh() {
  loading.value = true
  err.value = ''
  try {
    // 注意：变量名不要叫 h —— 上面从 vue 导入了渲染函数 h，会被遮蔽
    const [healthData, toolList] = await Promise.all([
      checkHealth(),
      listTools().catch(() => [] as ToolInfo[]),
    ])
    health.value = healthData as Health
    tools.value = toolList
    void loadWeChat()
    void loadAuthor()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

// ---------------------------------------------------------------- 公众号管理
const message = useMessage()
const wechatAccounts = ref<WeChatAccount[]>([])
const wechatLoading = ref(false)
const checkingId = ref('')

// 新增/编辑弹窗
const editShow = ref(false)
const editId = ref('') // 空 = 新增
const form = ref<WeChatAccountPayload>({
  name: '',
  appid: '',
  secret: '',
  homepage: '',
  gzh_name: '',
  slogan: '',
})
const formIsDefault = ref(false)
const saving = ref(false)

// 二维码上传（关注引导用）：新增时账号还没有 id，必须先保存再传
const qrUploading = ref(false)
const qrInputRef = ref<HTMLInputElement | null>(null)
const currentQrAccount = computed(() =>
  wechatAccounts.value.find((a) => a.id === editId.value),
)

// 占位符字面量（避免模板里写 `{{...}}` 被 Vue 当插值解析）
const ACCT_PH_NAME = '{{号名}}'
const ACCT_PH_SLOGAN = '{{slogan}}'

function qrPick() {
  qrInputRef.value?.click()
}

async function onQrcodePick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许重复选同一文件
  if (!file) return
  if (!editId.value) {
    message.warning('请先保存账号，再上传二维码')
    return
  }
  qrUploading.value = true
  try {
    await uploadWeChatQrcode(editId.value, file)
    message.success('二维码已上传')
    await loadWeChat()
  } catch (err) {
    message.error(`上传失败：${(err as Error).message}`)
  } finally {
    qrUploading.value = false
  }
}

async function loadWeChat() {
  wechatLoading.value = true
  try {
    wechatAccounts.value = await listWeChatAccounts()
  } catch (e) {
    // 后端可能还没起，静默即可（refresh 已经会报 err）
    console.warn('加载公众号列表失败', (e as Error).message)
  } finally {
    wechatLoading.value = false
  }
}

// ---------------------------------------------------------------- 作者信息
const authorName = ref('')
const authorIntro = ref('')
const authorLoading = ref(false)
const authorSaving = ref(false)

// 占位符字面量（放常量里，避免模板里写 `{{...}}` 被 Vue 当插值解析）
const AUTHOR_PH_NAME = '{{作者名}}'
const AUTHOR_PH_INTRO = '{{一句话简介}}'

async function loadAuthor() {
  authorLoading.value = true
  try {
    const a = await getAuthorInfo()
    authorName.value = a.author_name || ''
    authorIntro.value = a.author_intro || ''
  } catch (e) {
    console.warn('加载作者信息失败', (e as Error).message)
  } finally {
    authorLoading.value = false
  }
}

async function saveAuthor() {
  authorSaving.value = true
  try {
    await updateAuthorInfo({
      author_name: authorName.value.trim(),
      author_intro: authorIntro.value.trim(),
    })
    message.success('已保存')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    authorSaving.value = false
  }
}

function openCreate() {
  editId.value = ''
  form.value = { name: '', appid: '', secret: '', homepage: '', gzh_name: '', slogan: '' }
  formIsDefault.value = wechatAccounts.value.length === 0
  editShow.value = true
}

function openEdit(row: WeChatAccount) {
  editId.value = row.id
  // secret 不回显（后端不返回明文）；留空 = 不改
  form.value = {
    name: row.name,
    appid: row.appid,
    secret: '',
    homepage: row.homepage,
    gzh_name: row.gzh_name || '',
    slogan: row.slogan || '',
  }
  formIsDefault.value = row.is_default
  editShow.value = true
}

async function saveAccount() {
  if (!form.value.appid.trim()) {
    message.warning('请填 AppID')
    return
  }
  if (!editId.value && !form.value.secret.trim()) {
    message.warning('新增时必须填 AppSecret')
    return
  }
  saving.value = true
  try {
    if (editId.value) {
      await updateWeChatAccount(editId.value, { ...form.value })
      if (formIsDefault.value) await setDefaultWeChatAccount(editId.value)
      message.success('已更新')
    } else {
      await createWeChatAccount({ ...form.value, is_default: formIsDefault.value })
      message.success('已添加公众号')
    }
    editShow.value = false
    await loadWeChat()
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function onCheck(row: WeChatAccount) {
  checkingId.value = row.id
  try {
    const r = await checkWeChatAccount(row.id)
    if (r.ok) {
      message.success('连通正常，可推送草稿箱')
      await loadWeChat()
    } else {
      message.error(r.error || '验证失败')
    }
  } catch (e) {
    message.error(`验证失败：${(e as Error).message}`)
  } finally {
    checkingId.value = ''
  }
}

async function onSetDefault(row: WeChatAccount) {
  await setDefaultWeChatAccount(row.id)
  message.success(`已把「${row.name}」设为默认推送号`)
  await loadWeChat()
}

async function onDelete(row: WeChatAccount) {
  try {
    await deleteWeChatAccount(row.id)
    message.success('已删除')
    await loadWeChat()
  } catch (e) {
    message.error(`删除失败：${(e as Error).message}`)
  }
}

const wechatColumns = computed<DataTableColumns<WeChatAccount>>(() => [
  {
    title: '名称',
    key: 'name',
    render: (row) =>
      h('div', { style: 'display:flex;align-items:center;gap:6px' }, [
        row.name || row.appid,
        row.is_default
          ? h(NTag, { size: 'tiny', type: 'primary', bordered: false }, { default: () => '默认' })
          : null,
      ]),
  },
  { title: 'AppID', key: 'appid', width: 180, ellipsis: { tooltip: true } },
  {
    title: '主页',
    key: 'homepage',
    width: 160,
    ellipsis: { tooltip: true },
    render: (row) => row.homepage || h(NText, { depth: 3 }, { default: () => '—' }),
  },
  {
    title: '状态',
    key: 'verified_at',
    width: 100,
    render: (row) =>
      row.verified_at
        ? h(NTag, { size: 'tiny', type: 'success', bordered: false }, { default: () => '已验证' })
        : h(NTag, { size: 'tiny', bordered: false }, { default: () => '未验证' }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 250,
    render: (row) =>
      h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' }, [
        h(
          NButton,
          {
            size: 'tiny',
            loading: checkingId.value === row.id,
            onClick: () => onCheck(row),
          },
          { default: () => '测试连通' },
        ),
        !row.is_default
          ? h(
              NButton,
              { size: 'tiny', onClick: () => onSetDefault(row) },
              { default: () => '设为默认' },
            )
          : null,
        h(NButton, { size: 'tiny', onClick: () => openEdit(row) }, { default: () => '编辑' }),
        h(
          NButton,
          { size: 'tiny', quaternary: true, type: 'error', onClick: () => onDelete(row) },
          { default: () => '删除' },
        ),
      ]),
  },
])

const toolColumns: DataTableColumns<ToolInfo> = [
  {
    title: '工具名',
    key: 'name',
    width: 180,
    render: (row: ToolInfo) =>
      h('code', { style: 'font-size:12px' }, row.name),
  },
  {
    title: '说明',
    key: 'description',
    ellipsis: { tooltip: true },
  },
  {
    title: '确认方式',
    key: 'requires_confirm',
    width: 130,
    render: (row: ToolInfo) =>
      h(
        NTag,
        {
          size: 'small',
          bordered: false,
          type: row.requires_confirm ? 'warning' : 'success',
        },
        { default: () => (row.requires_confirm ? '需人工确认' : '可直接调用') },
      ),
  },
]

/** 环境变量清单：给部署同学对照用 */
interface EnvRow {
  key: string
  required: boolean
  desc: string
}

const ENV_ROWS: EnvRow[] = [
  { key: 'DEEPSEEK_API_KEY', required: true, desc: '模型调用密钥，缺了 AI 对话直接不可用' },
  { key: 'DEEPSEEK_BASE_URL', required: false, desc: '默认官方地址，走代理时才改' },
  { key: 'DEEPSEEK_MODEL_FLASH', required: false, desc: '默认 deepseek-flash，文案创作走这个' },
  { key: 'DEEPSEEK_MODEL_PRO', required: false, desc: '默认 deepseek-v4-pro，长文与逻辑推理走这个' },
  { key: 'DATABASE_URL', required: false, desc: '默认 sqlite+aiosqlite，上量后换 Postgres' },
  { key: 'REDIS_URL', required: false, desc: '留空自动降级内存缓存，多实例部署必须配' },
  { key: 'WECHAT_APP_ID', required: false, desc: '公众号 AppID，不配则草稿箱推送不可用' },
  { key: 'WECHAT_APP_SECRET', required: false, desc: '公众号密钥，与 AppID 成对出现' },
  { key: 'ENABLE_TOOLS', required: false, desc: '是否开启 Function Calling，默认开' },
  { key: 'TOOL_REQUIRE_CONFIRM', required: false, desc: '需要人工确认的工具名单，逗号分隔' },
]

const envColumns: DataTableColumns<EnvRow> = [
  {
    title: '变量名',
    key: 'key',
    width: 210,
    render: (row) => h('code', { style: 'font-size:12px' }, row.key),
  },
  {
    title: '必填',
    key: 'required',
    width: 76,
    render: (row) =>
      h(
        NTag,
        { size: 'tiny', bordered: false, type: row.required ? 'error' : 'default' },
        { default: () => (row.required ? '必填' : '可选') },
      ),
  },
  { title: '说明', key: 'desc' },
]
</script>

<template>
  <div>
    <NAlert type="info" :bordered="false" style="margin-bottom: 16px">
      这里能看到服务真实状态。<b>AI 未配置</b>通常只有两种原因：没填
      <code>DEEPSEEK_API_KEY</code>，或后端服务没起来。
    </NAlert>

    <NSpin :show="loading">
      <NAlert v-if="err" type="error" :bordered="false" style="margin-bottom: 16px">
        无法连接后端：{{ err }}。当前请求地址 <code>{{ apiBaseSaved }}</code
        ><template v-if="showApiBase">，可在下方「后端地址」里改成实际运行的后端</template>。
      </NAlert>

      <!-- 后端地址：仅静态部署 / 跨域访问时才需要改（dev server 有 proxy，不显示） -->
      <NCard v-if="showApiBase" size="small" title="后端地址" style="margin-bottom: 16px">
        <div class="api-base-row">
          <NInput
            v-model:value="apiBaseInput"
            placeholder="留空则用当前页面同源的 /api，例如 http://127.0.0.1:8000"
            size="small"
            @keydown.enter="applyApiBase"
          />
          <NButton size="small" type="primary" @click="applyApiBase">应用</NButton>
          <NButton size="small" :disabled="!usingCustom" @click="resetApiBase">
            恢复默认
          </NButton>
        </div>
        <div class="api-base-hint">
          当前生效：<code>{{ apiBaseSaved }}</code>
          <NTag v-if="usingCustom" size="small" type="warning" :bordered="false">自定义</NTag>
          <NTag v-else size="small" :bordered="false">默认同源</NTag>
          <span v-if="apiBaseMsg" class="api-base-msg">{{ apiBaseMsg }}</span>
        </div>
        <div class="api-base-hint" style="margin-top: 6px; opacity: 0.75">
          只填主机和端口就行（如 <code>127.0.0.1:8000</code>），<code>/api</code>
          会自动补上。改完立即生效，不用刷新。
        </div>
      </NCard>

      <!-- 账号：所有内容/队列/公众号账号/用量都按登录身份隔离 -->
      <NCard size="small" title="账号" style="margin-bottom: 16px">
        <div class="api-base-hint">
          当前账号：<code>{{ auth.user?.username }}</code>
          <NTag
            size="tiny"
            :bordered="false"
            :type="auth.isAdmin ? 'warning' : 'default'"
            style="margin-left: 6px"
          >
            {{ auth.isAdmin ? '管理员' : '普通成员' }}
          </NTag>
        </div>
        <div class="api-base-hint" style="margin-top: 6px; opacity: 0.75">
          注册时间：{{ createdAtText }}。内容、发布队列、公众号账号、AI
          用量、复刻音色都按登录身份隔离，互相不可见。
          <NButton size="tiny" style="margin-left: 8px" @click="onLogout">退出登录</NButton>
        </div>

        <!-- 我的配额：按钮级权限管控的几个花钱功能，还剩多少一眼可见 -->
        <div v-if="quotaRows.length" class="quota-list">
          <div class="quota-list-title">我的使用配额</div>
          <div v-for="q in quotaRows" :key="q.feature" class="quota-line">
            <span class="quota-name">{{ q.label }}</span>
            <span v-if="q.unlimited" class="quota-val muted">不限</span>
            <span v-else class="quota-val" :class="{ danger: q.left === 0 }">
              {{ q.used }} / {{ q.limit }}
              <span class="quota-period">（{{ q.periodLabel }}，{{ q.resetText }} 重置）</span>
            </span>
          </div>
          <div class="quota-note">
            超额会被直接拦下（HTTP 429）。次数按北京时间计算，提交即预扣 ——
            异步任务（渲染/排版/复刻）资源在提交时就已经花出去了，失败不退还；
            只有推送草稿能同步判成败，失败会自动退还。
          </div>
        </div>
      </NCard>

      <!-- 作者信息：排版时自动替换正文里的 {{作者名}} / {{一句话简介}} 占位符 -->
      <NCard size="small" title="作者信息" style="margin-bottom: 16px">
        <NSpin :show="authorLoading">
          <div class="author-form">
            <NInput
              v-model:value="authorName"
              size="small"
              style="width: 180px"
              placeholder="作者名（如：甲木）"
            />
            <NInput
              v-model:value="authorIntro"
              size="small"
              style="width: 340px"
              placeholder="一句话简介（如：专注 AI 观察与干货）"
            />
            <NButton size="small" type="primary" :loading="authorSaving" @click="saveAuthor">
              保存
            </NButton>
          </div>
        </NSpin>
        <div class="api-base-hint" style="margin-top: 8px; opacity: 0.75">
          排版时，正文里的
          <code v-text="AUTHOR_PH_NAME" /> 和 <code v-text="AUTHOR_PH_INTRO" />
          会自动替换成这里填的值。留空则不替换（保留占位符提醒你去填）。
          加工排版面板里也能临时改这一篇的作者信息。
        </div>
      </NCard>

      <div class="grid">
        <NCard size="small" title="服务健康">
          <NDescriptions :column="1" size="small" label-placement="left" bordered>
            <NDescriptionsItem label="服务状态">
              <NBadge
                :type="health?.status === 'ok' ? 'success' : 'error'"
                dot
                style="margin-right: 6px"
              />
              {{ health?.status ?? '未知' }}
            </NDescriptionsItem>
            <NDescriptionsItem label="运行环境">
              <NTag size="small" :bordered="false">{{ health?.env ?? '-' }}</NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="AI 模型">
              <NTag size="small" :type="health?.llm_configured ? 'success' : 'warning'" :bordered="false">
                {{ health?.llm_configured ? model : '未配置' }}
              </NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="缓存后端">
              <NTag size="small" :type="health?.redis ? 'success' : 'default'" :bordered="false">
                {{ health?.redis ? 'Redis' : '内存（单实例）' }}
              </NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="工具调用">
              <NTag size="small" :type="health?.tools_enabled ? 'success' : 'default'" :bordered="false">
                {{ health?.tools_enabled ? '已开启' : '已关闭' }}
              </NTag>
            </NDescriptionsItem>
          </NDescriptions>

          <NButton size="small" style="margin-top: 12px" @click="refresh">重新检测</NButton>
        </NCard>

        <NCard size="small" :title="`可用工具（${tools.length}）`">
          <NDataTable
            v-if="tools.length"
            :columns="toolColumns"
            :data="tools"
            :row-key="(r: ToolInfo) => r.name"
            size="small"
            :bordered="false"
            :max-height="300"
            :scroll-x="480"
          />
          <NText v-else depth="3" style="font-size: 13px">
            没有取到工具列表。检查 <code>ENABLE_TOOLS</code> 是否为 true。
          </NText>
          <div class="tip">
            标「需人工确认」的工具（推送草稿箱、提交渲染）不会由 AI 自动执行，
            必须在对话里点确认。这既是平台合规要求，也是账号安全底线。
          </div>
        </NCard>
      </div>

      <NCard size="small" title="公众号管理" style="margin-top: 16px">
        <template #header-extra>
          <NButton size="tiny" type="primary" @click="openCreate">添加公众号</NButton>
        </template>
        <NAlert type="info" :bordered="false" style="margin-bottom: 12px">
          <div style="font-size: 12.5px; line-height: 1.7">
            填 AppID / AppSecret 后即可把排版成品推到草稿箱。
            <b>务必把服务器出网 IP 加入公众号后台的「IP 白名单」</b>，否则会报 IP 未授权
            （本沙箱出网 IP 见下方提示）；一个都没配时回退 <code>.env</code> 的
            <code>WECHAT_APPID / WECHAT_SECRET</code>。
          </div>
        </NAlert>
        <NSpin :show="wechatLoading">
          <NDataTable
            v-if="wechatAccounts.length"
            :columns="wechatColumns"
            :data="wechatAccounts"
            :row-key="(r: WeChatAccount) => r.id"
            size="small"
            :bordered="false"
            :scroll-x="820"
          />
          <NText v-else depth="3" style="font-size: 13px">
            还没有公众号。点右上角「添加公众号」开始配置；也可继续用 .env 里的全局凭证。
          </NText>
        </NSpin>
      </NCard>

      <NModal
        v-model:show="editShow"
        preset="card"
        :title="editId ? '编辑公众号' : '添加公众号'"
        style="max-width: 520px"
      >
        <NForm label-placement="top" size="small">
          <NFormItem label="名称">
            <NInput v-model:value="form.name" placeholder="如：主号 · 内容运营" />
          </NFormItem>
          <NFormItem label="AppID" required>
            <NInput v-model:value="form.appid" placeholder="公众平台 → 设置与开发 → 基本配置" />
          </NFormItem>
          <NFormItem :label="editId ? 'AppSecret（留空 = 不改）' : 'AppSecret'">
            <NInput
              v-model:value="form.secret"
              type="password"
              show-password-on="click"
              placeholder="不填则沿用已保存的密钥"
            />
          </NFormItem>
          <NFormItem label="主页 / 简介（可选）">
            <NInput v-model:value="form.homepage" placeholder="公众号主页地址或一句话简介" />
          </NFormItem>

          <NDivider style="margin: 4px 0 12px" title-placement="left">
            <NText depth="3" style="font-size: 12px">关注引导（文末关注卡）</NText>
          </NDivider>
          <NFormItem :label="`公众号名（替换 ${ACCT_PH_NAME}）`">
            <NInput
              v-model:value="form.gzh_name"
              placeholder="对外展示的号名，如「码上聊聊」"
            />
          </NFormItem>
          <NFormItem :label="`Slogan（替换 ${ACCT_PH_SLOGAN}）`">
            <NInput v-model:value="form.slogan" placeholder="关注卡副标，如「有问题，码上聊聊」" />
          </NFormItem>
          <NFormItem label="公众号二维码（扫码 / 长按关注）">
            <div style="display: flex; align-items: center; gap: 12px">
              <img
                v-if="currentQrAccount?.qrcode_url"
                :src="currentQrAccount.qrcode_url"
                alt="公众号二维码"
                style="width: 76px; height: 76px; object-fit: cover; border-radius: 6px"
              />
              <NButton size="small" :loading="qrUploading" :disabled="!editId" @click="qrPick">
                {{ currentQrAccount?.qrcode_set ? '重新上传' : '上传二维码' }}
              </NButton>
              <NText v-if="!editId" depth="3" style="font-size: 12px">
                先保存账号，再传二维码
              </NText>
            </div>
            <input
              ref="qrInputRef"
              type="file"
              accept="image/png,image/jpeg,image/webp"
              style="display: none"
              @change="onQrcodePick"
            />
          </NFormItem>

          <NFormItem label="设为默认推送号">
            <NSwitch v-model:value="formIsDefault" />
          </NFormItem>
        </NForm>
        <template #footer>
          <div style="display:flex;justify-content:flex-end;gap:8px">
            <NButton size="small" @click="editShow = false">取消</NButton>
            <NButton size="small" type="primary" :loading="saving" @click="saveAccount">
              保存
            </NButton>
          </div>
        </template>
      </NModal>

      <NCard size="small" title="环境变量对照表" style="margin-top: 16px">
        <NDataTable
          :columns="envColumns"
          :data="ENV_ROWS"
          :row-key="(r: EnvRow) => r.key"
          size="small"
          :bordered="false"
          :max-height="330"
          :scroll-x="520"
        />
      </NCard>

      <NCard size="small" title="成本提示" style="margin-top: 16px">
        <ul class="cost-list">
          <li>
            <b>峰谷计费：</b>工作日北京时间 09:00–12:00、14:00–18:00 为高峰，价格约翻倍。
            批量生成长文建议排到低谷时段。
          </li>
          <li>
            <b>缓存命中：</b>系统提示词固定放最前，DeepSeek 的 Prompt Cache 命中后输入价降至
            1/50。所以不要频繁改动 <code>app/prompts/platforms.py</code> 的措辞。
          </li>
          <li>
            <b>字幕与配音：</b>用 edge-tts 本地合成（句级时间轴即字幕轴），零 API 成本，
            只有画面素材生成才可能产生费用。
          </li>
        </ul>
      </NCard>
    </NSpin>
  </div>
</template>

<style scoped>
.api-base-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.api-base-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-2);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.api-base-msg {
  color: var(--app-success-text);
}

.author-form {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.tip {
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.75;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--app-border-60);
}

.cost-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 2;
  color: var(--app-text-2);
}

.cost-list b {
  color: var(--app-text);
}

/* 我的配额 */
.quota-list {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--app-border-60);
}

.quota-list-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-2);
  margin-bottom: 8px;
}

.quota-line {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  line-height: 1.9;
}

.quota-name {
  min-width: 72px;
  color: var(--app-text-2);
}

.quota-val {
  font-variant-numeric: tabular-nums;
}

.quota-val.danger {
  color: #d03050;
  font-weight: 600;
}

.quota-val.muted {
  color: var(--app-text-3);
}

.quota-period {
  font-size: 12px;
  color: var(--app-text-3);
}

.quota-note {
  margin-top: 8px;
  font-size: 12px;
  color: var(--app-text-3);
  line-height: 1.7;
}

@media (max-width: 900px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
