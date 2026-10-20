<script setup lang="ts">
/**
 * 系统设置页。
 * 只讲「这套系统怎么样」—— 服务健康、可用工具、公众号凭证、环境变量
 * 对照、成本提示。**和「我」相关的设置**（账号资料、使用配额、作者
 * 信息、AI 对话偏好）在个人中心（顶栏头像下拉进入），不在这里。
 */
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { actionBtn } from '@/utils/actionBtn'
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
import { fetchZhihuUsage, saveZhihuSecret, type ZhihuUsage } from '@/api/zhihu'
import { useAuthStore } from '@/stores/auth'
import IconTextBtn from '@/components/IconTextBtn.vue'
import {
  checkWeChatAccount,
  createWeChatAccount,
  deleteWeChatAccount,
  getChatProvider,
  listWeChatAccounts,
  setDefaultWeChatAccount,
  updateWeChatAccount,
  uploadWeChatQrcode,
  type ChatProviderInfo,
  type WeChatAccountPayload,
} from '@/api/settings'
import type { ToolInfo, WeChatAccount } from '@/types'

interface Health {
  status: string
  env: string
  llm_configured: boolean
  redis: boolean
  tools_enabled: boolean
  /** DeepSeek Flash（文案/轻任务/视觉走这个） */
  model_flash: string
  /** DeepSeek Pro（复杂推理走这个） */
  model_pro: string
  /** 主模型失败时的兜底模型，空 = 不兜底 */
  fallback_model: string
  /** CF 通道 .env 默认模型（用户在个人资料页改过的值不在这里） */
  cf_chat_model: string
  /** CF 凭证是否就位 */
  cf_configured: boolean
}

const health = ref<Health | null>(null)
const tools = ref<ToolInfo[]>([])
const loading = ref(true)
const err = ref('')

const auth = useAuthStore()

// 用户当前对话通道（个人资料页可改，DB 优先于 .env）——
// 服务健康的「AI 模型」要显示用户改后的实际模型，只读 health 的
// .env 配置不够，得和这个拼起来才是真相。
const chatProvider = ref<ChatProviderInfo>({ provider: 'deepseek', model: '', cloudflare_ready: false })
const activeCfModel = computed(() => chatProvider.value.model || health.value?.cf_chat_model || '')

// 走 vite dev server 时 /api 已由 proxy 转发到后端，手动指定地址是噪音；
// 只有生产构建（dist 静态部署、没有 dev proxy）时才需要暴露这个配置。
const showApiBase = import.meta.env.PROD

// ---- 后端地址（静态部署或跨域访问时手动指定）----
const apiBaseInput = ref(getApiBase())
const apiBaseSaved = ref(getApiBase())
const apiBaseMsg = ref('')

const usingCustom = computed(() => isCustomApiBase())

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
    void loadZhihu()
    void loadChatProvider()
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

/** 当前对话通道（用户在个人资料页改的 provider + CF 模型）。
 *  失败不阻塞服务健康卡片 —— 回落默认 deepseek，最坏是显示 .env 口径。 */
async function loadChatProvider() {
  try {
    chatProvider.value = await getChatProvider()
  } catch {
    /* 通道信息加载失败不该拖垮整个系统设置页，静默用默认值 */
  }
}

// ---------------------------------------------------------------- 知乎热点
// 额度余量 + 在线配 Access Secret（2026-10-19 管理后台需求）。
// 两个端点都是 require_admin，卡片同样按 isAdmin 门控 —— 双保险，
// 非管理员连那个注定 403 的请求都不会发出。
const zhihuUsage = ref<ZhihuUsage | null>(null)
const zhihuLoading = ref(false)
const zhihuSecretInput = ref('')
const zhihuSaving = ref(false)

// 移动端断点与 AppShell / AdminUsersView 保持一致（768px）。
// 初始值在 script 顶层就算好 —— 首渲染早于 onMounted，等挂载后再赋值
// 会让移动端先闪一帧桌面布局。知乎卡片的数据行含长文本（如
// 「已用 6 / 100，剩余 94」「进行中（水位 88）」），桌面 2 列在窄屏下
// 会被挤压换行、把行高撑高，移动端切单列 + label 置顶即可。
const isMobile = ref(
  typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches,
)

async function loadZhihu() {
  if (!auth.isAdmin) return
  zhihuLoading.value = true
  try {
    zhihuUsage.value = await fetchZhihuUsage()
  } catch {
    /* 额度面板加载失败不该拖垮整个系统设置页，静默留空 */
  } finally {
    zhihuLoading.value = false
  }
}

async function saveSecret() {
  zhihuSaving.value = true
  try {
    zhihuUsage.value = await saveZhihuSecret(zhihuSecretInput.value)
    zhihuSecretInput.value = ''
    const f = zhihuUsage.value.fetch
    if (f?.ok) message.success(`凭证已保存，顺手抓到 ${f.saved ?? 0} 条热点`)
    else if (f && !f.ok) message.error(`凭证已保存，但抓取失败：${f.error || '未知错误'}`)
    else message.success('凭证已清除，热点灵感将停更')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  } finally {
    zhihuSaving.value = false
  }
}

const zhihuLastFetch = computed(() => {
  const u = zhihuUsage.value
  if (!u || !u.last_fetch_at) return '从未抓取'
  const d = new Date(u.last_fetch_at * 1000)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const label =
    d.toDateString() === new Date().toDateString()
      ? `今天 ${hm}`
      : `${d.getMonth() + 1}/${d.getDate()} ${hm}`
  return u.last_ok ? `${label} · 成功 ${u.last_item_count} 条` : `${label} · 失败`
})

onMounted(() => {
  const mq = window.matchMedia('(max-width: 767px)')
  isMobile.value = mq.matches
  const onMqChange = (e: MediaQueryListEvent) => {
    isMobile.value = e.matches
  }
  mq.addEventListener('change', onMqChange)
  onUnmounted(() => mq.removeEventListener('change', onMqChange))
  refresh()
})

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
      h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, [
        actionBtn('link', '测试连通', () => onCheck(row), { loading: checkingId.value === row.id }),
        !row.is_default ? actionBtn('star', '设为默认', () => onSetDefault(row)) : null,
        actionBtn('edit', '编辑', () => openEdit(row)),
        actionBtn('trash', '删除', () => onDelete(row), { type: 'error' }),
      ].filter(Boolean)),
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
  { key: 'ZHIHU_ACCESS_SECRET', required: false, desc: '知乎开放平台密钥；也可在上方「知乎热点」卡片里在线设置（DB 优先）' },
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
          <IconTextBtn name="check" type="primary" @click="applyApiBase">应用</IconTextBtn>
          <IconTextBtn name="reset" :disabled="!usingCustom" @click="resetApiBase">
            恢复默认
          </IconTextBtn>
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

      <!-- 用户级设置（账号资料 / 使用配额 / 作者信息 / AI 对话偏好）
           已搬到「个人中心」—— 顶栏头像下拉进入 -->

      <div class="grid">
        <NCard size="small" title="服务健康">
          <!-- label 位置响应式：移动端 label 置顶，内容（通道 tag + 模型名 +
               分工注释）吃满行宽，不与 label 互相挤压把行撑高 -->
          <NDescriptions
            :column="1"
            size="small"
            :label-placement="isMobile ? 'top' : 'left'"
            bordered
          >
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
              <template v-if="health?.llm_configured">
                <div class="ai-model-cell">
                  <NTag
                    size="small"
                    :bordered="false"
                    :type="chatProvider.provider === 'cloudflare' ? 'info' : 'success'"
                  >
                    {{ chatProvider.provider === 'cloudflare' ? 'Cloudflare 通道' : 'DeepSeek 通道' }}
                  </NTag>
                  <span class="ai-model-name">
                    <template v-if="chatProvider.provider === 'cloudflare'">
                      {{ activeCfModel || '未选择模型' }}
                    </template>
                    <template v-else>
                      {{ health.model_flash }}<span class="ai-model-sep">·</span>{{ health.model_pro }}
                    </template>
                  </span>
                </div>
                <div class="ai-model-note">
                  <template v-if="chatProvider.provider === 'cloudflare'">
                    个人资料页所选；该通道不支持对话内工具调用
                  </template>
                  <template v-else>
                    Flash 管文案/轻任务，Pro 管复杂推理<template v-if="health.fallback_model">；兜底 {{ health.fallback_model }}</template>
                  </template>
                </div>
              </template>
              <NTag v-else size="small" :bordered="false" type="warning">未配置</NTag>
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

          <IconTextBtn name="reset" style="margin-top: 12px" @click="refresh">重新检测</IconTextBtn>
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
          <IconTextBtn name="plus" size="tiny" type="primary" @click="openCreate">添加公众号</IconTextBtn>
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
              <IconTextBtn name="upload" size="small" :loading="qrUploading" :disabled="!editId" @click="qrPick">
                {{ currentQrAccount?.qrcode_set ? '重新上传' : '上传二维码' }}
              </IconTextBtn>
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
            <IconTextBtn name="check" type="primary" :loading="saving" @click="saveAccount">
              保存
            </IconTextBtn>
          </div>
        </template>
      </NModal>

      <!-- 知乎热点：额度可视 + 在线配 Access Secret。端点 require_admin，
           卡片同样按 isAdmin 门控（双保险，非管理员不发出注定 403 的请求）。 -->
      <NCard v-if="auth.isAdmin" size="small" title="知乎热点" style="margin-top: 16px">
        <NSpin :show="zhihuLoading">
          <NDescriptions
            :column="isMobile ? 1 : 2"
            size="small"
            :label-placement="isMobile ? 'top' : 'left'"
            bordered
          >
            <NDescriptionsItem label="Access Secret">
              <NTag
                v-if="zhihuUsage?.configured"
                size="small"
                :bordered="false"
                type="success"
              >
                已配置 {{ zhihuUsage.masked }}
              </NTag>
              <NTag v-else size="small" :bordered="false" type="warning">未配置</NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="今日额度（热榜）">
              <template v-if="zhihuUsage">
                已用 {{ zhihuUsage.used }} / {{ zhihuUsage.quota }}，剩余
                <b>{{ zhihuUsage.remaining }}</b>
              </template>
              <span v-else>-</span>
            </NDescriptionsItem>
            <NDescriptionsItem label="自动抓取">
              <NTag
                size="small"
                :bordered="false"
                :type="zhihuUsage?.safe_to_fetch ? 'success' : 'warning'"
              >
                {{
                  zhihuUsage?.safe_to_fetch
                    ? `进行中（水位 ${zhihuUsage.safe_limit}）`
                    : '已停（今日用量触达水位）'
                }}
              </NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="抓取间隔">
              每 {{ zhihuUsage?.interval_minutes ?? 30 }} 分钟
            </NDescriptionsItem>
            <NDescriptionsItem label="上次抓取">
              {{ zhihuLastFetch }}
            </NDescriptionsItem>
          </NDescriptions>

          <NDivider style="margin: 4px 0 12px" />
          <div class="zhihu-secret-row">
            <NInput
              v-model:value="zhihuSecretInput"
              type="password"
              show-password-on="click"
              size="small"
              placeholder="粘贴知乎开放平台的 Access Secret"
              @keydown.enter="saveSecret"
            />
            <IconTextBtn
              name="save"
              type="primary"
              size="small"
              :loading="zhihuSaving"
              @click="saveSecret"
            >
              保存并抓一次
            </IconTextBtn>
          </div>
          <div class="tip">
            同账号下所有 Secret 共享同一个每日额度池（在开放平台页面上的测试调用也扣）。
            保存后立即抓一次热榜用于验证凭证，失败会显示在「上次抓取」里。
            <b>留空保存 = 清除凭证</b>，热点灵感将停更。在后台保存过之后，
            这里的值优先于 <code>.env</code> 的 <code>ZHIHU_ACCESS_SECRET</code>。
          </div>
        </NSpin>
      </NCard>

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

/* 知乎 Secret 输入行：输入框吃满宽度，保存按钮不缩 */
.zhihu-secret-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

/* AI 模型行：通道 tag + 模型名一行，分工注解放第二行小字 */
.ai-model-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.ai-model-name {
  font-size: 13px;
}

.ai-model-sep {
  color: var(--app-text-3);
  margin: 0 2px;
}

.ai-model-note {
  font-size: 12px;
  color: var(--app-text-2);
  margin-top: 2px;
  line-height: 1.5;
}

.zhihu-secret-row .n-input {
  flex: 1;
  min-width: 0;
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

/* 我的配额 / 作者信息 / AI 对话偏好的样式随卡片搬去了 ProfileView.vue */

@media (max-width: 900px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
