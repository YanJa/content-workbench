<script setup lang="ts">
/**
 * 模型池管理页。
 *
 * --------------------------------------------------------------------------
 * 这一页回答四个问题，顺序就是四个 Tab 的顺序
 * --------------------------------------------------------------------------
 *   1. 每个功能现在用哪个模型？（功能绑定）—— 最常用，放第一个
 *   2. 有哪些服务商、凭证配好没？（服务商）
 *   3. 池里有哪些模型、额度用了多少？（模型）
 *   4. 我想用自己的 Key 吗？（我的 Key）
 *
 * --------------------------------------------------------------------------
 * 两条刻意不为的边界
 * --------------------------------------------------------------------------
 *   - **不做「自动挑最便宜的模型」**。选择权交给管理员：免费模型的质量、
 *     延迟、QPS 各不相同，系统猜不出「这篇稿子值不值得用好模型」。
 *   - **不做 per-user 绑定**。功能用哪个模型是系统策略，个人能覆盖的只有
 *     凭证（用自己的 Key = 烧自己的额度），不是策略。
 *
 * 非管理员能进这一页（读端点登录即可），但所有写按钮都隐藏 —— 他要能看到
 * 「现在用的是什么模型」，改不了就改不了，不该装作能改。
 */
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NNumberAnimation,
  NSelect,
  NSpace,
  NStatistic,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  NText,
  NTooltip,
  useDialog,
  useMessage,
  type DataTableColumns,
  type FormInst,
} from 'naive-ui'

import IconTextBtn from '@/components/IconTextBtn.vue'
import { actionBtn } from '@/utils/actionBtn'
import { useAuthStore } from '@/stores/auth'
import {
  CAPABILITY_LABEL,
  PROTOCOL_LABEL,
  VENDOR_TEMPLATES,
  bulkCreateCatalog,
  createCatalog,
  createProvider,
  deleteCatalog,
  deleteProvider,
  listBindings,
  listCatalog,
  listMyKeys,
  listProviders,
  poolMeta,
  poolQuota,
  saveBinding,
  saveMyKey,
  syncProvider,
  testProvider,
  updateCatalog,
  updateProvider,
  verifyMyKey,
  type ProviderPayload,
} from '@/api/modelPool'
import type {
  ModelBinding,
  ModelCatalogEntry,
  ModelProviderRow,
  MyProviderKey,
  QuotaRow,
  SyncCandidate,
} from '@/types'
import type { VendorTemplate } from '@/api/modelPool'

const message = useMessage()
const dialog = useDialog()
const auth = useAuthStore()

const isAdmin = computed(() => auth.isAdmin)

const tab = ref<'bindings' | 'providers' | 'catalog' | 'keys'>('bindings')
const loading = ref(false)

const providers = ref<ModelProviderRow[]>([])
const catalog = ref<ModelCatalogEntry[]>([])
const bindings = ref<ModelBinding[]>([])
const quota = ref<QuotaRow[]>([])
const myKeys = ref<MyProviderKey[]>([])
const featureNames = ref<Record<string, string>>({})

async function load() {
  loading.value = true
  try {
    const [p, c, b, q, k, meta] = await Promise.all([
      listProviders(),
      listCatalog(),
      listBindings(),
      poolQuota(),
      listMyKeys(),
      poolMeta(),
    ])
    providers.value = p
    catalog.value = c
    bindings.value = b
    quota.value = q
    myKeys.value = k
    featureNames.value = Object.fromEntries(meta.features.map((f) => [f.key, f.name]))
  } catch (e) {
    message.error((e as Error).message || '加载模型池失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

/* ------------------------------------------------------------------ 窄屏
 * 这页的表格列多，手机上一定放不下。两个补丁：
 *   - 表格给 scroll-x，让它自己横向滚（全局 body 是 overflow-x:hidden，
 *     不设 scroll-x 的话表格会被直接裁掉，连滑都滑不动）；
 *   - 操作列在窄屏 fixed 到右侧，否则每次要点「编辑/删除」都得先滑到最右。
 */
const isMobile = ref(typeof window !== 'undefined' && window.innerWidth < 768)
function syncViewport() {
  isMobile.value = window.innerWidth < 768
}
onMounted(() => window.addEventListener('resize', syncViewport))
onUnmounted(() => window.removeEventListener('resize', syncViewport))

/** 失败提示统一走这里：后端不少校验文案是长句，截断会丢关键信息 */
function errText(e: unknown, fallback: string) {
  return (e as Error)?.message || fallback
}

// ---------------------------------------------------------------- 概览数字

const statFree = computed(() => catalog.value.filter((c) => c.billing === 'free').length)
const statPaid = computed(() => catalog.value.filter((c) => c.billing === 'paid').length)
const statBound = computed(() => bindings.value.filter((b) => b.primary).length)
const statBreaker = computed(() => catalog.value.filter((c) => c.disabled_for > 0).length)

// ---------------------------------------------------------------- 功能绑定

/** 按能力过滤可选模型：生图槽不该能选到 chat 模型（选了必然报错） */
function modelsForFeature(feature: string): { label: string; value: string }[] {
  const want = feature === 'image' ? 'image' : feature === 'embedding' ? 'embedding' : 'chat'
  return catalog.value
    .filter((c) => (c.capabilities || []).includes(want))
    .map((c) => ({
      label: `${c.label || c.model_id}　[${c.provider_key}]${c.billing === 'free' ? '　免费' : ''}`,
      value: c.id,
    }))
}

const bindingDraft = ref<Record<string, { primary: string; fallbacks: string[] }>>({})

function draftOf(b: ModelBinding) {
  if (!bindingDraft.value[b.feature]) {
    // 兜底剔除历史脏数据：主模型若曾被存进备选链，取草稿时先清掉
    bindingDraft.value[b.feature] = {
      primary: b.primary,
      fallbacks: (b.fallbacks || []).filter((f) => f !== b.primary),
    }
  }
  return bindingDraft.value[b.feature]
}

function dirty(b: ModelBinding): boolean {
  const d = bindingDraft.value[b.feature]
  if (!d) return false
  return d.primary !== b.primary || d.fallbacks.join(',') !== b.fallbacks.join(',')
}

/**
 * 保存绑定。
 *
 * 换 embedding 模型会撞 409（detail 是对象，带 stale_chunks）—— 这不是
 * 「操作失败」，是「需要你确认代价」。所以这里弹二次确认，确认后带
 * force=true 重发，并在成功后提示去重建索引。
 */
async function onSaveBinding(b: ModelBinding) {
  const d = draftOf(b)
  try {
    await saveBinding(b.feature, d.primary, d.fallbacks)
    message.success(`「${b.name}」已保存`)
    await load()
  } catch (e) {
    const err = e as Error & { detail?: { code?: string; stale_chunks?: number; to_model?: string } }
    if (err.detail?.code === 'EMBED_DIM_MISMATCH') {
      const n = err.detail.stale_chunks ?? 0
      dialog.warning({
        title: '换向量化模型会让已有索引失效',
        content: `库里有 ${n} 条已就绪的向量是用旧模型算的，换了之后它们不再参与检索（不报错，只是搜不到）。是否继续并立即重建？`,
        positiveText: '继续并重建',
        negativeText: '取消',
        onPositiveClick: async () => {
          try {
            await saveBinding(b.feature, d.primary, d.fallbacks, true)
            message.success('已切换，请在「知识库」页点「重建索引」')
            await load()
          } catch (e2) {
            message.error(errText(e2, '切换失败'))
          }
        },
      })
      return
    }
    message.error(errText(e, '保存失败'))
  }
}

const bindingColumns = computed<DataTableColumns<ModelBinding>>(() => [
  {
    title: '功能',
    key: 'feature',
    width: 160,
    render: (r) =>
      h('div', [
        h('div', { style: 'font-weight:600' }, r.name),
        h('div', { style: 'font-size:12px;opacity:.6' }, r.no_fallback ? '不支持备选' : r.task),
      ]),
  },
  {
    title: '主模型',
    key: 'primary',
    minWidth: 240,
    render: (r) =>
      isAdmin.value
        ? h(NSelect, {
            value: draftOf(r).primary,
            options: modelsForFeature(r.feature),
            placeholder: '未绑定（回落 .env 通道）',
            clearable: true,
            filterable: true,
            onUpdateValue: (v: string) => {
              const p = v ?? ''
              draftOf(r).primary = p
              // 主模型若原本在备选链里，从备选链移除：主模型优先级最高，
              // 留着只会在界面上显示成裸 id（选项已被过滤掉，标签拿不到 label）
              if (p) draftOf(r).fallbacks = draftOf(r).fallbacks.filter((f) => f !== p)
            },
          })
        : h('span', r.primary_label || r.primary || '未绑定'),
  },
  {
    title: '备选链',
    key: 'fallbacks',
    minWidth: 240,
    render: (r) => {
      if (r.no_fallback) {
        return h('span', { style: 'font-size:12px;opacity:.6' }, '—')
      }
      if (!isAdmin.value) return h('span', { style: 'font-size:12px' }, r.fallbacks.length ? `${r.fallbacks.length} 个` : '无')
      return h(NSelect, {
        value: draftOf(r).fallbacks,
        multiple: true,
        options: modelsForFeature(r.feature).filter((o) => o.value !== draftOf(r).primary),
        placeholder: '额度用尽 / 报错时依次尝试',
        onUpdateValue: (v: string[]) => {
          draftOf(r).fallbacks = v ?? []
        },
      })
    },
  },
  {
    title: '实际会用',
    key: 'resolved',
    minWidth: 160,
    render: (r) =>
      r.resolved
        ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => r.resolved_label || r.resolved)
        : h(NTag, { size: 'small', type: 'warning', bordered: false }, () => '回落 .env 通道'),
  },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    fixed: isMobile.value ? 'right' : undefined,
    render: (r) =>
      isAdmin.value
        ? h(
            NButton,
            {
              size: 'small',
              type: 'primary',
              disabled: !dirty(r),
              onClick: () => onSaveBinding(r),
            },
            { default: () => '保存' },
          )
        : null,
  },
])

// ---------------------------------------------------------------- 服务商

const provShow = ref(false)
const provEditing = ref<ModelProviderRow | null>(null)
const provForm = ref<ProviderPayload>({ key: '', name: '', kind: 'openai', base_url: '' })
const provTesting = ref(false)

function openProvCreate() {
  provEditing.value = null
  provForm.value = {
    key: '',
    name: '',
    kind: 'openai',
    base_url: '',
    api_key: '',
    enabled: true,
    quota_reset_tz: 'cst',
    billing_default: 'paid',
  }
  provShow.value = true
}

function openProvEdit(row: ModelProviderRow) {
  provEditing.value = row
  provForm.value = {
    key: row.key,
    name: row.name,
    kind: row.kind,
    base_url: row.base_url,
    enabled: row.enabled,
    note: row.note,
    quota_reset_tz: row.quota_reset_tz,
    billing_default: row.billing_default || 'paid',
    daily_calls_limit: row.daily_calls_limit,
    daily_token_limit: row.daily_token_limit,
    monthly_token_limit: row.monthly_token_limit,
    rpm_limit: row.rpm_limit,
  }
  provShow.value = true
}

/** 选厂商模板：只做「填表」，不提交 —— 用户还得填 Key，也可能需要改地址 */
function applyTemplate(key: string) {
  const t = VENDOR_TEMPLATES.find((v) => v.key === key)
  if (!t) return
  provForm.value = {
    ...provForm.value,
    key: t.key,
    name: t.name,
    kind: t.kind,
    base_url: t.base_url,
    quota_reset_tz: t.tz,
    note: t.hint,
  }
}

async function onSaveProvider() {
  try {
    if (provEditing.value) {
      await updateProvider(provEditing.value.key, provForm.value)
      message.success('已保存')
    } else {
      await createProvider(provForm.value)
      message.success('已添加服务商')
    }
    provShow.value = false
    await load()
  } catch (e) {
    message.error(errText(e, '保存失败'))
  }
}

async function onTestProvider(row: ModelProviderRow) {
  provTesting.value = true
  try {
    const r = await testProvider(row.key)
    message.success(`连通正常，可访问 ${r.count} 个模型：${r.sample.slice(0, 3).join('、')}`)
  } catch (e) {
    message.error(errText(e, '连通性测试失败'))
  } finally {
    provTesting.value = false
  }
}

async function onToggleProvider(row: ModelProviderRow, val: boolean) {
  try {
    await updateProvider(row.key, { enabled: val })
    row.enabled = val
    await load()
  } catch (e) {
    message.error(errText(e, '切换失败'))
  }
}

function onDeleteProvider(row: ModelProviderRow) {
  dialog.warning({
    title: `删除服务商「${row.name}」？`,
    content: '会连带删掉它下面的全部模型条目，并清空引用它们的绑定。此操作不可撤销。',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const n = await deleteProvider(row.key)
        message.success(`已删除，连带 ${n} 个模型条目`)
        await load()
      } catch (e) {
        message.error(errText(e, '删除失败'))
      }
    },
  })
}

const provColumns = computed<DataTableColumns<ModelProviderRow>>(() => [
  {
    title: '服务商',
    key: 'name',
    minWidth: 200,
    render: (r) =>
      h('div', [
        h(NSpace, { size: 4, wrap: false, align: 'center' }, () => [
          h('div', { style: 'font-weight:600' }, r.name),
          r.billing_default === 'free'
            ? h(NTag, { size: 'tiny', bordered: false, type: 'success' }, () => '免费通道')
            : null,
        ]),
        h('div', { style: 'font-size:12px;opacity:.6' }, `${r.key} · ${r.kind}`),
      ]),
  },
  { title: '端点', key: 'base_url', minWidth: 260, ellipsis: { tooltip: true } },
  {
    title: 'Key',
    key: 'api_key',
    width: 150,
    render: (r) =>
      r.has_key
        ? h(NTag, { size: 'small', type: 'success', bordered: false }, () => r.api_key)
        : h(NTag, { size: 'small', type: 'error', bordered: false }, () => '未配置'),
  },
  {
    title: '日额度重置',
    key: 'tz',
    width: 100,
    render: (r) => (r.quota_reset_tz === 'utc' ? '00:00 UTC' : '00:00 北京'),
  },
  {
    title: '启用',
    key: 'enabled',
    width: 70,
    render: (r) =>
      h(NSwitch, {
        value: r.enabled,
        disabled: !isAdmin.value,
        onUpdateValue: (v: boolean) => onToggleProvider(r, v),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 210,
    fixed: isMobile.value ? 'right' : undefined,
    render: (r) =>
      isAdmin.value
        ? h(NSpace, { size: 2, wrap: false, align: 'center' }, () => [
            actionBtn('check', '连通性测试', () => onTestProvider(r), { size: 'small' }),
            actionBtn('reset', '同步模型清单', () => openSync(r.key), { size: 'small' }),
            actionBtn('edit', '编辑', () => openProvEdit(r), { size: 'small' }),
            actionBtn('trash', '删除', () => onDeleteProvider(r), { size: 'small', type: 'error' }),
          ])
        : null,
  },
])

// ---------------------------------------------------------------- 同步模型清单

const syncShow = ref(false)
const syncKey = ref('')
const syncItems = ref<SyncCandidate[]>([])
const syncChecked = ref<string[]>([])
const syncLoading = ref(false)
const syncSaving = ref(false)

async function openSync(key: string) {
  syncKey.value = key
  syncShow.value = true
  syncLoading.value = true
  syncItems.value = []
  syncChecked.value = []
  try {
    syncItems.value = await syncProvider(key)
  } catch (e) {
    message.error(errText(e, '拉取模型清单失败'))
    syncShow.value = false
  } finally {
    syncLoading.value = false
  }
}

/** 默认只勾「池里还没有的」，但允许全选 —— 有的模型是被误删后要加回来 */
const syncOptions = computed(() =>
  syncItems.value.map((i) => ({
    label: `${i.model_id}　[${(i.capabilities || []).map((c) => CAPABILITY_LABEL[c] || c).join('/')}]`,
    value: i.model_id,
    disabled: i.exists,
  })),
)

function pickNewOnes() {
  syncChecked.value = syncItems.value.filter((i) => !i.exists).map((i) => i.model_id)
}

async function onBulkAdd() {
  syncSaving.value = true
  try {
    const picked = syncItems.value.filter((i) => syncChecked.value.includes(i.model_id))
    const n = await bulkCreateCatalog(syncKey.value, picked)
    message.success(n ? `已入库 ${n} 个模型` : '没有新模型入库')
    syncShow.value = false
    await load()
  } catch (e) {
    message.error(errText(e, '入库失败'))
  } finally {
    syncSaving.value = false
  }
}

// ---------------------------------------------------------------- 模型条目

const catShow = ref(false)
const catEditing = ref<ModelCatalogEntry | null>(null)
const catForm = ref<Record<string, unknown>>({})

function openCatCreate() {
  catEditing.value = null
  catForm.value = {
    provider_key: providers.value[0]?.key ?? '',
    model_id: '',
    label: '',
    billing: 'free',
    protocol: 'openai_chat',
    capabilities: ['chat'],
    supports_tools: true,
    supports_stream_usage: true,
    quota_scope: 'model',
    enabled: true,
  }
  catShow.value = true
}

function openCatEdit(row: ModelCatalogEntry) {
  catEditing.value = row
  catForm.value = {
    label: row.label,
    protocol: row.protocol,
    capabilities: row.capabilities,
    dimensions: row.dimensions,
    billing: row.billing,
    supports_tools: row.supports_tools,
    supports_stream_usage: row.supports_stream_usage,
    quota_scope: row.quota_scope,
    daily_calls_limit: row.daily_calls_limit,
    daily_token_limit: row.daily_token_limit,
    monthly_token_limit: row.monthly_token_limit,
    rpm_limit: row.rpm_limit,
    enabled: row.enabled,
    note: row.note,
  }
  catShow.value = true
}

async function onSaveCatalog() {
  try {
    if (catEditing.value) {
      await updateCatalog(catEditing.value.id, catForm.value)
      message.success('已保存')
    } else {
      await createCatalog(catForm.value as never)
      message.success('已添加模型')
    }
    catShow.value = false
    await load()
  } catch (e) {
    message.error(errText(e, '保存失败'))
  }
}

async function onUnbreak(row: ModelCatalogEntry) {
  try {
    await updateCatalog(row.id, { disabled_until: 0 })
    message.success('已解除熔断')
    await load()
  } catch (e) {
    message.error(errText(e, '操作失败'))
  }
}

function onDeleteCatalog(row: ModelCatalogEntry) {
  dialog.warning({
    title: `从池中移除「${row.label || row.model_id}」？`,
    content: '会同时把它从所有功能槽的绑定里摘掉。',
    positiveText: '移除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteCatalog(row.id)
        message.success('已移除')
        await load()
      } catch (e) {
        message.error(errText(e, '移除失败'))
      }
    },
  })
}

function usageText(row: ModelCatalogEntry): string {
  const u = row.usage || {}
  const calls = u.used_day_calls
  const lim = u.limit_day_calls
  if (calls == null && lim == null) return '—'
  return lim ? `${calls ?? 0} / ${lim}` : `${calls ?? 0}`
}

const catColumns = computed<DataTableColumns<ModelCatalogEntry>>(() => [
  {
    title: '模型',
    key: 'model_id',
    minWidth: 220,
    render: (r) =>
      h('div', [
        h(NSpace, { size: 4, wrap: false, align: 'center' }, () => [
          h('span', { style: 'font-weight:600' }, r.label || r.model_id),
          h(
            NTag,
            { size: 'tiny', bordered: false, type: r.billing === 'free' ? 'success' : 'default' },
            () => (r.billing === 'free' ? '免费' : '付费'),
          ),
        ]),
        h('div', { style: 'font-size:12px;opacity:.6' }, `${r.provider_key} · ${r.model_id}`),
      ]),
  },
  {
    title: '能力',
    key: 'capabilities',
    width: 130,
    render: (r) =>
      h(NSpace, { size: 4, wrap: true }, () =>
        (r.capabilities || []).map((c) =>
          h(NTag, { size: 'tiny', bordered: false }, () => CAPABILITY_LABEL[c] || c),
        ),
      ),
  },
  {
    title: '协议',
    key: 'protocol',
    minWidth: 170,
    render: (r) => h('span', { style: 'font-size:12px' }, PROTOCOL_LABEL[r.protocol] || r.protocol),
  },
  {
    title: '今日用量',
    key: 'usage',
    width: 110,
    render: (r) => {
      const ratio = r.usage?.ratio ?? 0
      const txt = usageText(r)
      if (ratio >= 0.8 && ratio < 1) {
        return h(NTooltip, null, { trigger: () => h(NText, { type: 'warning' }, () => txt), default: () => `已用 ${Math.round(ratio * 100)}%` })
      }
      if (ratio >= 1) {
        return h(NTooltip, null, { trigger: () => h(NText, { type: 'error' }, () => txt), default: () => '额度已用尽，正在跳过' })
      }
      return h('span', { style: 'font-size:12px;opacity:.7' }, txt)
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 130,
    render: (r) => {
      if (r.disabled_for > 0) {
        return h(
          NTooltip,
          null,
          {
            trigger: () =>
              h(NTag, { size: 'small', type: 'error', bordered: false }, () => `熔断 ${Math.ceil(r.disabled_for / 60)} 分钟`),
            default: () => r.last_error || '最近一次调用失败',
          },
        )
      }
      if (!r.enabled) return h(NTag, { size: 'small', bordered: false }, () => '已停用')
      return h(NTag, { size: 'small', type: 'success', bordered: false }, () => '可用')
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 190,
    fixed: isMobile.value ? 'right' : undefined,
    render: (r) =>
      isAdmin.value
        ? h(NSpace, { size: 2, wrap: false, align: 'center' }, () => [
            ...(r.disabled_for > 0
              ? [actionBtn('reset', '解除熔断', () => onUnbreak(r), { size: 'small' })]
              : []),
            actionBtn('edit', '编辑', () => openCatEdit(r), { size: 'small' }),
            actionBtn('trash', '从池中移除', () => onDeleteCatalog(r), { size: 'small', type: 'error' }),
          ])
        : null,
  },
])

// ---------------------------------------------------------------- 额度

const quotaColumns = computed<DataTableColumns<QuotaRow>>(() => [
  {
    title: '模型',
    key: 'model',
    minWidth: 200,
    render: (r) =>
      h('div', [
        h('div', r.label || r.model),
        h('div', { style: 'font-size:12px;opacity:.6' }, `${r.provider} · ${r.model}`),
      ]),
  },
  {
    title: '凭证归属',
    key: 'cred_owner',
    width: 120,
    render: (r) =>
      r.cred_owner === 'system'
        ? h(NTag, { size: 'small', bordered: false }, () => '平台 Key')
        : h(NTag, { size: 'small', type: 'info', bordered: false }, () => '个人 Key'),
  },
  {
    title: '今日调用',
    key: 'day_calls',
    width: 120,
    render: (r) => {
      const used = r.used?.day_calls ?? 0
      const lim = r.limits?.day_calls
      return h('span', lim ? `${used} / ${lim}` : String(used))
    },
  },
  {
    title: '今日 tokens',
    key: 'day_tokens',
    width: 130,
    render: (r) => {
      const used = r.used?.day_tokens ?? 0
      const lim = r.limits?.day_tokens
      return h('span', lim ? `${used} / ${lim}` : String(used))
    },
  },
  {
    title: '本月 tokens',
    key: 'month_tokens',
    width: 130,
    render: (r) => {
      const used = r.used?.month_tokens ?? 0
      const lim = r.limits?.month_tokens
      return h('span', lim ? `${used} / ${lim}` : String(used))
    },
  },
])

// ---------------------------------------------------------------- 我的 Key

const keyDraft = ref<Record<string, string>>({})
const keySaving = ref<Record<string, boolean>>({})

async function onSaveMyKey(row: MyProviderKey) {
  const val = (keyDraft.value[row.provider_key] ?? '').trim()
  keySaving.value[row.provider_key] = true
  try {
    if (!val) {
      await saveMyKey(row.provider_key, '')
      message.success(`已回退到平台 Key（${row.provider_name}）`)
    } else {
      await saveMyKey(row.provider_key, val)
      message.success(`已保存，${row.provider_name} 将烧你自己的额度`)
    }
    keyDraft.value[row.provider_key] = ''
    await load()
  } catch (e) {
    message.error(errText(e, '保存失败'))
  } finally {
    keySaving.value[row.provider_key] = false
  }
}

async function onVerifyMyKey(row: MyProviderKey) {
  try {
    const n = await verifyMyKey(row.provider_key)
    message.success(`验证通过，可访问 ${n} 个模型`)
    await load()
  } catch (e) {
    message.error(errText(e, '验证失败'))
  }
}

const keyColumns = computed<DataTableColumns<MyProviderKey>>(() => [
  {
    title: '服务商',
    key: 'provider_name',
    minWidth: 180,
    render: (r) =>
      h('div', [
        h('div', r.provider_name),
        h('div', { style: 'font-size:12px;opacity:.6' }, r.provider_key),
      ]),
  },
  {
    title: '当前状态',
    key: 'status',
    width: 180,
    render: (r) =>
      r.using_my_key
        ? h(NTag, { size: 'small', type: 'info', bordered: false }, () => `我的 Key ${r.api_key}`)
        : h('span', { style: 'font-size:12px;opacity:.6' }, '用平台 Key'),
  },
  {
    title: '填我自己的 Key（留空 = 回退平台 Key）',
    key: 'input',
    minWidth: 280,
    render: (r) =>
      h(NInput, {
        value: keyDraft.value[r.provider_key] ?? '',
        type: 'password',
        showPasswordOn: 'click',
        placeholder: r.using_my_key ? '已保存，重新输入即替换' : '粘贴你的 API Key',
        onUpdateValue: (v: string) => {
          keyDraft.value[r.provider_key] = v
        },
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 160,
    fixed: isMobile.value ? 'right' : undefined,
    render: (r) =>
      h(NSpace, { size: 6, wrap: false, align: 'center' }, () => [
        h(
          NButton,
          {
            size: 'small',
            type: 'primary',
            loading: keySaving.value[r.provider_key],
            onClick: () => onSaveMyKey(r),
          },
          { default: () => '保存' },
        ),
        ...(r.using_my_key
          ? [actionBtn('check', '验证这个 Key 能不能用', () => onVerifyMyKey(r), { size: 'small' })]
          : []),
      ]),
  },
])

const vendorOptions = computed(() =>
  VENDOR_TEMPLATES.map((t: VendorTemplate) => ({ label: t.name, value: t.key })),
)
const providerOptions = computed(() =>
  providers.value.map((p) => ({ label: `${p.name}（${p.key}）`, value: p.key })),
)
const capabilityOptions = [
  { label: '对话', value: 'chat' },
  { label: '视觉', value: 'vision' },
  { label: '生图', value: 'image' },
  { label: '向量', value: 'embedding' },
]
const protocolOptions = computed(() =>
  Object.entries(PROTOCOL_LABEL).map(([v, l]) => ({ label: l, value: v })),
)

const provFormRef = ref<FormInst | null>(null)
</script>

<template>
  <div class="model-pool">
    <NAlert v-if="!isAdmin" type="info" :bordered="false" style="margin-bottom: 12px">
      你是只读身份：能看「现在用的是什么模型」，但改不了。需要改动请联系管理员。
    </NAlert>

    <NSpace align="center" justify="space-between" style="margin-bottom: 12px">
      <NSpace :size="24" align="center">
        <NStatistic label="功能槽已绑定">
          <NNumberAnimation :from="0" :to="statBound" /> / {{ bindings.length }}
        </NStatistic>
        <NStatistic label="免费模型"><NNumberAnimation :from="0" :to="statFree" /></NStatistic>
        <NStatistic label="付费模型"><NNumberAnimation :from="0" :to="statPaid" /></NStatistic>
        <NStatistic label="熔断中">
          <NText :type="statBreaker ? 'error' : 'default'">
            <NNumberAnimation :from="0" :to="statBreaker" />
          </NText>
        </NStatistic>
      </NSpace>
    </NSpace>

    <NText v-if="isMobile" depth="3" style="display: block; margin-bottom: 8px; font-size: 12px">
      表格较宽，可左右滑动查看；操作按钮固定在最右侧。
    </NText>

    <NTabs v-model:value="tab" type="line" animated>
      <!-- 1. 功能绑定 -->
      <NTabPane name="bindings" tab="功能绑定">
        <NAlert type="default" :bordered="false" style="margin-bottom: 12px">
          每个功能槽单独选模型：写稿用好模型、打标签用便宜的、向量化和生图各走各的。
          配了备选链之后，主模型额度用尽或报错会自动往下切（已产出正文的错误不切换，
          免得半句话换模型）。「知识库向量化」不允许备选 —— 换向量模型会让已有索引失效。
        </NAlert>
        <NDataTable
          :columns="bindingColumns"
          :data="bindings"
          :loading="loading"
          :bordered="false"
          size="small"
          :scroll-x="900"
          :row-key="(r: ModelBinding) => r.feature"
        />
      </NTabPane>

      <!-- 2. 服务商 -->
      <NTabPane name="providers" tab="服务商">
        <NSpace justify="space-between" style="margin-bottom: 12px">
          <NText depth="3" style="font-size: 13px">
            服务商 = 一家厂的凭证 + 端点。选个模板就自动填好地址，粘 Key 即可。
          </NText>
          <IconTextBtn v-if="isAdmin" name="plus" type="primary" size="small" @click="openProvCreate">
            新增服务商
          </IconTextBtn>
        </NSpace>
        <NDataTable
          :columns="provColumns"
          :data="providers"
          :loading="loading"
          :bordered="false"
          size="small"
          :scroll-x="1000"
          :row-key="(r: ModelProviderRow) => r.key"
        />
      </NTabPane>

      <!-- 3. 模型 -->
      <NTabPane name="catalog" tab="模型">
        <NSpace justify="space-between" style="margin-bottom: 12px">
          <NText depth="3" style="font-size: 13px">
            池里的模型。建议先在「服务商」页点「同步」批量拉清单，再回来逐个调额度与开关。
          </NText>
          <IconTextBtn v-if="isAdmin" name="plus" type="primary" size="small" @click="openCatCreate">
            手动添加
          </IconTextBtn>
        </NSpace>
        <NDataTable
          :columns="catColumns"
          :data="catalog"
          :loading="loading"
          :bordered="false"
          size="small"
          :scroll-x="960"
          :row-key="(r: ModelCatalogEntry) => r.id"
        />

        <NCard title="额度用量" size="small" style="margin-top: 16px">
          <NEmpty v-if="!quota.length" description="还没有任何调用记录" />
          <NDataTable
            v-else
            :columns="quotaColumns"
            :data="quota"
            :bordered="false"
            size="small"
            :scroll-x="720"
            :row-key="(r: QuotaRow) => `${r.cred_owner}-${r.model}`"
          />
        </NCard>
      </NTabPane>

      <!-- 4. 我的 Key -->
      <NTabPane name="keys" tab="我的 Key">
        <NAlert type="default" :bordered="false" style="margin-bottom: 12px">
          填自己的 Key 之后，这家服务商的模型都烧你自己的额度，不占公共额度。
          留空保存即回退到平台 Key。出于安全，已保存的 Key 只显示首尾各 4 位。
        </NAlert>
        <NDataTable
          :columns="keyColumns"
          :data="myKeys"
          :loading="loading"
          :bordered="false"
          size="small"
          :scroll-x="820"
          :row-key="(r: MyProviderKey) => r.provider_key"
        />
      </NTabPane>
    </NTabs>

    <!-- 服务商表单 -->
    <NModal
      v-model:show="provShow"
      preset="card"
      :title="provEditing ? '编辑服务商' : '新增服务商'"
      style="width: 640px; max-width: 90vw"
    >
      <NForm ref="provFormRef" :label-placement="isMobile ? 'top' : 'left'" label-width="110">
        <NFormItem v-if="!provEditing" label="厂商模板">
          <NSelect
            :options="vendorOptions"
            placeholder="选一个自动填好地址（也可完全手填）"
            filterable
            clearable
            @update:value="applyTemplate"
          />
        </NFormItem>
        <NFormItem label="标识 key">
          <NInput
            v-model:value="provForm.key"
            :disabled="!!provEditing"
            placeholder="如 zhipu / siliconflow，英文短标识"
          />
        </NFormItem>
        <NFormItem label="名称">
          <NInput v-model:value="provForm.name" placeholder="展示名" />
        </NFormItem>
        <NFormItem label="类型">
          <NSelect
            v-model:value="provForm.kind"
            :options="[
              { label: 'OpenAI 兼容', value: 'openai' },
              { label: 'Cloudflare Workers AI', value: 'cloudflare' },
            ]"
          />
        </NFormItem>
        <NFormItem label="端点 base_url">
          <NInput v-model:value="provForm.base_url" placeholder="https://..." />
        </NFormItem>
        <NFormItem label="API Key">
          <NInput
            v-model:value="provForm.api_key"
            type="password"
            show-password-on="click"
            :placeholder="provEditing ? '留空表示不改动' : '粘贴 Key'"
          />
        </NFormItem>
        <NFormItem label="日额度重置">
          <NSelect
            v-model:value="provForm.quota_reset_tz"
            :options="[
              { label: '北京时间 00:00（国内厂商）', value: 'cst' },
              { label: 'UTC 00:00（Cloudflare / OpenRouter）', value: 'utc' },
            ]"
          />
        </NFormItem>
        <NFormItem label="默认计费">
          <NSelect
            v-model:value="provForm.billing_default"
            :options="[
              { label: '免费通道（记账 0 元）', value: 'free' },
              { label: '付费（按单价表算）', value: 'paid' },
            ]"
          />
          <template #feedback>
            只在这家下面的模型**自己没标**计费性质时兜底。整家都是免费档（Cloudflare
            这类）选「免费通道」，它家没进池的模型也不会被按兜底价记成钱。
          </template>
        </NFormItem>
        <NFormItem label="账号级护栏">
          <NSpace :size="8" align="center">
            <NInputNumber
              v-model:value="provForm.daily_calls_limit"
              placeholder="日调用上限"
              :min="0"
              style="width: 130px"
            />
            <NInputNumber
              v-model:value="provForm.rpm_limit"
              placeholder="每分钟上限"
              :min="0"
              style="width: 130px"
            />
            <NText depth="3" style="font-size: 12px">留空 = 不限。免费档建议填 RPM</NText>
          </NSpace>
        </NFormItem>
        <NFormItem label="备注">
          <NInput v-model:value="provForm.note" type="textarea" :rows="2" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton size="small" @click="provShow = false">取消</NButton>
          <NButton size="small" type="primary" :loading="provTesting" @click="onSaveProvider">
            保存
          </NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- 同步模型清单 -->
    <NModal
      v-model:show="syncShow"
      preset="card"
      title="同步模型清单"
      style="width: 680px; max-width: 90vw"
    >
      <NSpace vertical :size="10">
        <NText depth="3" style="font-size: 13px">
          从 {{ syncKey }} 拉到的模型清单。默认只勾池里还没有的；灰掉的是已存在。
        </NText>
        <NSpace>
          <NButton size="tiny" :disabled="!syncItems.length" @click="pickNewOnes">
            只勾新增
          </NButton>
          <NButton
            size="tiny"
            :disabled="!syncItems.length"
            @click="syncChecked = syncItems.filter((i) => !i.exists).length ? [] : syncItems.map((i) => i.model_id)"
          >
            反选
          </NButton>
        </NSpace>
        <NSelect
          v-model:value="syncChecked"
          multiple
          filterable
          :options="syncOptions"
          :loading="syncLoading"
          placeholder="选择要入库的模型"
          :max-tag-count="12"
          style="max-height: 320px; overflow: auto"
        />
        <NText v-if="syncItems.length" depth="3" style="font-size: 12px">
          共 {{ syncItems.length }} 个（已存在 {{ syncItems.filter((i) => i.exists).length }} 个）
        </NText>
      </NSpace>
      <template #footer>
        <NSpace justify="end">
          <NButton size="small" @click="syncShow = false">取消</NButton>
          <NButton
            size="small"
            type="primary"
            :loading="syncSaving"
            :disabled="!syncChecked.length"
            @click="onBulkAdd"
          >
            入库（{{ syncChecked.length }}）
          </NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- 模型表单 -->
    <NModal
      v-model:show="catShow"
      preset="card"
      :title="catEditing ? '编辑模型' : '手动添加模型'"
      style="width: 640px; max-width: 90vw"
    >
      <NForm :label-placement="isMobile ? 'top' : 'left'" label-width="110">
        <template v-if="!catEditing">
          <NFormItem label="服务商">
            <NSelect v-model:value="(catForm as any).provider_key" :options="providerOptions" />
          </NFormItem>
          <NFormItem label="模型 ID">
            <NInput v-model:value="(catForm as any).model_id" placeholder="厂商原始模型名，如 glm-4-flash" />
          </NFormItem>
        </template>
        <NFormItem label="展示名">
          <NInput v-model:value="(catForm as any).label" />
        </NFormItem>
        <NFormItem label="能力">
          <NSelect v-model:value="(catForm as any).capabilities" multiple :options="capabilityOptions" />
        </NFormItem>
        <NFormItem label="协议">
          <NSelect v-model:value="(catForm as any).protocol" :options="protocolOptions" />
        </NFormItem>
        <NFormItem label="计费">
          <NSelect
            v-model:value="(catForm as any).billing"
            :options="[
              { label: '免费', value: 'free' },
              { label: '付费', value: 'paid' },
            ]"
          />
        </NFormItem>
        <NFormItem label="额度口径">
          <NSelect
            v-model:value="(catForm as any).quota_scope"
            :options="[
              { label: '模型级（这个模型自己一份）', value: 'model' },
              { label: '账号级（与同账号其它模型共享）', value: 'provider' },
            ]"
          />
        </NFormItem>
        <NFormItem label="维度">
          <NInputNumber
            v-model:value="(catForm as any).dimensions"
            placeholder="仅向量化模型需要"
            :min="0"
            style="width: 160px"
          />
        </NFormItem>
        <NFormItem label="能力开关">
          <NSpace :size="16">
            <NText depth="3" style="font-size: 12px">工具调用</NText>
            <NSwitch v-model:value="(catForm as any).supports_tools" />
            <NText depth="3" style="font-size: 12px">流式 usage</NText>
            <NSwitch v-model:value="(catForm as any).supports_stream_usage" />
          </NSpace>
        </NFormItem>
        <NFormItem label="额度上限">
          <NSpace :size="8" align="center">
            <NInputNumber v-model:value="(catForm as any).daily_calls_limit" placeholder="日调用" :min="0" style="width: 120px" />
            <NInputNumber v-model:value="(catForm as any).daily_token_limit" placeholder="日 token" :min="0" style="width: 120px" />
            <NInputNumber v-model:value="(catForm as any).rpm_limit" placeholder="每分钟" :min="0" style="width: 120px" />
          </NSpace>
        </NFormItem>
        <NFormItem label="启用">
          <NSwitch v-model:value="(catForm as any).enabled" />
        </NFormItem>
        <NFormItem label="备注">
          <NInput v-model:value="(catForm as any).note" type="textarea" :rows="2" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NSpace justify="end">
          <NButton size="small" @click="catShow = false">取消</NButton>
          <NButton size="small" type="primary" @click="onSaveCatalog">保存</NButton>
        </NSpace>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.model-pool {
  padding: 4px 0;
}
</style>
