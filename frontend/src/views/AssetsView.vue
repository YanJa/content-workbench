<script setup lang="ts">
/**
 * 素材库页面。
 *
 * 页面职责与「选取弹窗」分开：这里是**管理**（看全部、整理目录、改标签、批量操作、删、看授权信息），
 * 弹窗是**取用**（写文章时挑一张插进去）。同一个后端，两种用法。
 *
 * 一条合规设计：卡片上始终显示许可徽标。素材库最容易出的事不是技术问题，
 * 是「这张图能不能商用」记不清了 —— 让它在界面上一直可见，比藏在详情里可靠。
 *
 * 三种查看方式共用同一份 items 数据，只换渲染：
 *   grid  = 缩略图墙，快速扫图
 *   flat  = 平铺列表，看元数据、批量管理
 *   tree  = 文件夹+素材混排展开，一眼看库结构
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NCheckbox,
  NDataTable,
  NDropdown,
  NEmpty,
  NInput,
  NModal,
  NPagination,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpin,
  NTag,
  NTree,
  NUpload,
  useDialog,
  useMessage,
  type DataTableColumns,
  type TreeOption,
  type UploadCustomRequestOptions,
} from 'naive-ui'

import {
  assetStats,
  assetTree,
  createAssetFolder,
  deleteAsset,
  deleteAssetFolder,
  downloadAssetsZip,
  importCandidate,
  listAssetFolders,
  listAssetSources,
  listAssets,
  moveAssets,
  searchOnline,
  updateAsset,
  updateAssetFolder,
  uploadAssetBatch,
  type Asset,
  type AssetCandidate,
  type AssetFolder,
  type AssetSourceStatus,
  type AssetStats,
  type AssetTreeNode,
} from '@/api/assets'
import { usePermGate } from '@/composables/usePermGate'

const message = useMessage()
const dialog = useDialog()
const deleteGate = usePermGate({ perm: 'assets.delete' })
const importGate = usePermGate({ perm: 'assets.import' })

const items = ref<Asset[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 30
const loading = ref(false)
const keyword = ref('')
const kindFilter = ref<string>('')
const sourceFilter = ref<string>('')
const stats = ref<AssetStats | null>(null)

// 空串 = 不限。用 '' 而不是 null，是为了让 NSelect 的类型与 v-model 一致
// （混用 null 与 string 时 NSelect 的 value 类型会退化成 any）。
const kindOptions = [
  { label: '全部类型', value: '' },
  { label: '图片', value: 'image' },
  { label: '视频', value: 'video' },
]
const sourceOptions = [
  { label: '全部来源', value: '' },
  { label: '手动上传', value: 'upload' },
  { label: '在线导入', value: 'search' },
  { label: 'AI 生成', value: 'ai' },
  { label: '封面', value: 'cover' },
]

// ---------------- 目录（folder） ----------------
const folders = ref<AssetFolder[]>([])
const uncategorizedCount = ref(0)
/** 当前选中的目录节点：'all' 全部 / 'none' 未归类 / 'f:<id>' 某目录 */
const folderSel = ref<string>('all')
const treeData = ref<TreeOption[]>([])

function buildTreeOptions(list: AssetFolder[]): TreeOption[] {
  const byParent = new Map<string | null, AssetFolder[]>()
  for (const f of list) {
    const p = f.parent_id || null
    const arr = byParent.get(p) || []
    arr.push(f)
    byParent.set(p, arr)
  }
  const build = (parent: string | null): TreeOption[] =>
    (byParent.get(parent) || []).map((f) => ({
      key: `f:${f.id}`,
      label: `${f.name} (${f.asset_count ?? 0})`,
      children: build(f.id),
    }))
  return build(null)
}

async function loadFolders() {
  try {
    const r = await listAssetFolders()
    folders.value = r.items
    uncategorizedCount.value = r.uncategorized_count
    treeData.value = [
      { key: 'all', label: '全部素材' },
      { key: 'none', label: `未归类 (${r.uncategorized_count})` },
      ...buildTreeOptions(r.items),
    ]
  } catch {
    // 目录加载失败不阻塞列表（列表还能用），静默即可
  }
}

function onTreeSelect(keys: string[]) {
  folderSel.value = keys[0] ?? 'all'
  page.value = 1
  void reload()
}

// 当前生效的 folder_id 过滤参数：'all' → 不过滤
const folderParam = computed(() => {
  if (folderSel.value === 'all') return undefined
  if (folderSel.value === 'none') return 'none'
  return folderSel.value.slice(2)
})

/** 当前选中目录的「真实 id」（''=未归类，null=全部）。批量上传/移动时用。 */
const folderTargetId = computed<string | null>(() => {
  if (folderSel.value === 'all' || folderSel.value === 'none') return null
  return folderSel.value.slice(2)
})

// ---------------- 目录 CRUD ----------------
const folderModalShow = ref(false)
const folderModalMode = ref<'create' | 'rename'>('create')
const folderModalParent = ref<string | null>(null) // create 时的父目录
const folderModalTarget = ref<AssetFolder | null>(null) // rename 时的目标
const folderName = ref('')

function openCreateFolder(parentId: string | null = null) {
  folderModalMode.value = 'create'
  folderModalParent.value = parentId
  folderModalTarget.value = null
  folderName.value = ''
  folderModalShow.value = true
}

function openRenameFolder(f: AssetFolder) {
  folderModalMode.value = 'rename'
  folderModalTarget.value = f
  folderName.value = f.name
  folderModalShow.value = true
}

async function saveFolder() {
  const name = folderName.value.trim()
  if (!name) {
    message.warning('目录名不能为空')
    return
  }
  try {
    if (folderModalMode.value === 'create') {
      await createAssetFolder(name, folderModalParent.value)
      message.success('已创建目录')
    } else if (folderModalTarget.value) {
      await updateAssetFolder(folderModalTarget.value.id, { name })
      message.success('已重命名')
    }
    folderModalShow.value = false
    await loadFolders()
  } catch (e: unknown) {
    const detailMsg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detailMsg || '操作失败')
  }
}

function removeFolder(f: AssetFolder) {
  const n = f.asset_count ?? 0
  dialog.warning({
    title: `删除目录「${f.name}」`,
    content: n
      ? `该目录（含子目录）下有 ${n} 个素材。删除后它们会移到上一级目录，不会丢。`
      : '该目录（含子目录）下没有素材，直接删除。',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteAssetFolder(f.id, 'move-to-parent')
        message.success('已删除目录，素材已移到上一级')
        if (folderSel.value === `f:${f.id}`) {
          folderSel.value = 'all'
          await reload()
        }
        await loadFolders()
      } catch {
        message.error('删除失败')
      }
    },
  })
}

const folderMenuOptions = [
  { label: '新建子目录', key: 'new' },
  { label: '重命名', key: 'rename' },
  { label: '删除', key: 'delete' },
]

function onFolderMenu(key: string, f: AssetFolder) {
  if (key === 'new') openCreateFolder(f.id)
  else if (key === 'rename') openRenameFolder(f)
  else if (key === 'delete') removeFolder(f)
}

// ---------------- 视图切换（三种） ----------------
type ViewMode = 'grid' | 'flat' | 'tree'
const VIEW_KEY = 'assets.viewMode'
const viewMode = ref<ViewMode>(
  ((localStorage.getItem(VIEW_KEY) as ViewMode) || 'grid') satisfies ViewMode,
)

function onViewModeChange(v: ViewMode) {
  viewMode.value = v
  localStorage.setItem(VIEW_KEY, v)
  if (v === 'tree') void loadTree()
}

// ---------------- 树形视图数据 ----------------
const treeNodes = ref<AssetTreeNode[]>([])
const treeUncategorized = ref<Asset[]>([])
const treeLoading = ref(false)

async function loadTree() {
  treeLoading.value = true
  try {
    const r = await assetTree()
    treeNodes.value = r.folders
    treeUncategorized.value = r.uncategorized
  } catch {
    message.error('目录树加载失败')
  } finally {
    treeLoading.value = false
  }
}

// ---------------- 多选 ----------------
const selectedIds = ref<Set<string>>(new Set())
const checkedKeys = ref<string[]>([])

function toggleSelect(id: string) {
  const s = new Set(selectedIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  selectedIds.value = s
  checkedKeys.value = [...s]
}

function selectAllPage() {
  const s = new Set(selectedIds.value)
  for (const a of items.value) s.add(a.id)
  selectedIds.value = s
  checkedKeys.value = [...s]
}

function clearSelect() {
  selectedIds.value = new Set()
  checkedKeys.value = []
}

function onCheckedRowKeysChange(keys: Array<string | number>) {
  // 树形视图的行 key 带前缀（asset:<id>），这里只认 asset 行
  const s = new Set<string>()
  for (const k of keys) {
    const ks = String(k)
    s.add(ks.startsWith('asset:') ? ks.slice(6) : ks)
  }
  selectedIds.value = s
  checkedKeys.value = keys.map(String)
}

const selectedCount = computed(() => selectedIds.value.size)

// ---------------- 批量操作 ----------------
const moveModalShow = ref(false)
const moveTarget = ref<string | null>(null)
const moveOptions = computed(() => [
  { label: '未归类', value: '__none__' },
  ...folders.value.map((f) => ({ label: f.name, value: f.id })),
])
const moving = ref(false)

async function doBatchMove() {
  if (!selectedIds.value.size) return
  moving.value = true
  try {
    const target = moveTarget.value === '__none__' ? null : moveTarget.value
    const r = await moveAssets([...selectedIds.value], target)
    message.success(`已移动 ${r.moved} 个素材`)
    moveModalShow.value = false
    clearSelect()
    await refreshAll()
    if (viewMode.value === 'tree') await loadTree()
  } catch {
    message.error('移动失败')
  } finally {
    moving.value = false
  }
}

async function doBatchDownload() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  if (ids.length > 200) {
    message.warning('一次最多打包 200 个素材，先少选一些')
    return
  }
  try {
    await downloadAssetsZip(ids)
    message.success('已开始下载 zip')
  } catch {
    message.error('打包下载失败')
  }
}

function doBatchDelete() {
  if (deleteGate.blocked.value) {
    message.warning(deleteGate.reason.value)
    return
  }
  const n = selectedIds.value.size
  if (!n) return
  dialog.warning({
    title: '批量删除素材',
    content: `确定删除选中的 ${n} 个素材？删除后它们在文章里的配图会失效。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      let ok = 0
      for (const id of selectedIds.value) {
        try {
          await deleteAsset(id)
          ok += 1
        } catch {
          // 单个失败不中断：用户要的是这一批尽量删掉
        }
      }
      message.success(`已删除 ${ok} 个素材`)
      clearSelect()
      await refreshAll()
      if (viewMode.value === 'tree') await loadTree()
    },
  })
}

// ---------------- 详情 / 编辑 ----------------
const detail = ref<Asset | null>(null)
const detailShow = ref(false)
const editTitle = ref('')
const editTags = ref<string[]>([])
const saving = ref(false)

// ---------------- 在线搜图 ----------------
const searchShow = ref(false)
const sq = ref('')
const sItems = ref<AssetCandidate[]>([])
const sLoading = ref(false)
const sImporting = ref('')
const sources = ref<AssetSourceStatus[]>([])
const searched = ref(false)
/** 各图源的失败原因，空数组 = 没有源失败 */
const sErrors = ref<{ source: string; reason: string }[]>([])

const uploading = ref(false)

const usagePct = computed(() => {
  if (!stats.value || !stats.value.quota_bytes) return 0
  return Math.min(100, (stats.value.used_bytes / stats.value.quota_bytes) * 100)
})

const usageText = computed(() => {
  if (!stats.value) return ''
  const mb = (n: number) => (n / 1048576).toFixed(n >= 10485760 ? 0 : 1)
  return `${mb(stats.value.used_bytes)}MB / ${mb(stats.value.quota_bytes)}MB`
})

async function reload() {
  loading.value = true
  try {
    const r = await listAssets({
      kind: kindFilter.value || undefined,
      source: sourceFilter.value || undefined,
      q: keyword.value.trim() || undefined,
      folder_id: folderParam.value,
      page: page.value,
      page_size: pageSize,
    })
    items.value = r.items
    total.value = r.total
  } catch {
    message.error('素材列表加载失败')
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await assetStats()
  } catch {
    stats.value = null
  }
}

async function refreshAll() {
  await Promise.all([reload(), loadStats(), loadFolders()])
}

function onSearch() {
  page.value = 1
  void reload()
}

async function doUploadBatch({ file, onFinish, onError }: UploadCustomRequestOptions) {
  const f = file.file
  if (!f) {
    onError()
    return
  }
  uploading.value = true
  try {
    const r = await uploadAssetBatch([f], folderTargetId.value)
    if (r.failed.length) message.warning(`${f.name} 上传失败：${r.failed[0].reason}`)
    else message.success(r.items.length ? `已上传到${folderTargetId.value ? '当前目录' : '未归类'}` : '库里已有这张')
    onFinish()
    page.value = 1
    await refreshAll()
    if (viewMode.value === 'tree') await loadTree()
  } catch (e: unknown) {
    const detailMsg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detailMsg || '上传失败')
    onError()
  } finally {
    uploading.value = false
  }
}

function openDetail(a: Asset) {
  detail.value = a
  editTitle.value = a.title
  editTags.value = [...(a.tags || [])]
  detailShow.value = true
}

async function saveDetail() {
  if (!detail.value) return
  saving.value = true
  try {
    const r = await updateAsset(detail.value.id, {
      title: editTitle.value.trim(),
      tags: editTags.value,
    })
    const idx = items.value.findIndex((x) => x.id === r.asset.id)
    if (idx >= 0) items.value[idx] = r.asset
    message.success('已保存')
    detailShow.value = false
  } catch {
    message.error('保存失败')
  } finally {
    saving.value = false
  }
}

function removeAsset(a: Asset) {
  if (deleteGate.blocked.value) {
    message.warning(deleteGate.reason.value)
    return
  }
  const extra = a.used_count
    ? `它已被插入文章 ${a.used_count} 次 —— 删除后那些文章的配图会失效。`
    : ''
  dialog.warning({
    title: '删除素材',
    content: `确定删除「${a.title || '未命名素材'}」？${extra}`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const r = await deleteAsset(a.id)
        message.success(r.referenced ? `已删除（有 ${r.referenced} 处引用可能失效）` : '已删除')
        detailShow.value = false
        if (items.value.length === 1 && page.value > 1) page.value -= 1
        await refreshAll()
        if (viewMode.value === 'tree') await loadTree()
      } catch {
        message.error('删除失败')
      }
    },
  })
}

// ---------------- 在线搜图 ----------------
async function loadSources() {
  try {
    sources.value = (await listAssetSources()).sources
  } catch {
    sources.value = []
  }
}

async function doSearch() {
  const q = sq.value.trim()
  if (!q) {
    message.warning('输入关键词再搜')
    return
  }
  sLoading.value = true
  searched.value = true
  sErrors.value = []
  try {
    const r = await searchOnline({ q, source: 'all', per_page: 24 })
    sItems.value = r.items
    sources.value = r.sources
    sErrors.value = r.errors || []
    if (r.items.length) return
    // 空结果分两种：源挂了 vs 真没图。混为一谈会让人一直换关键词白试。
    if (sErrors.value.length) {
      // 面板里已有错误卡，不重复弹
    } else if (!sources.value.some((s) => s.configured)) {
      message.warning('还没有可用的图源，先配一个 API Key')
    } else {
      message.info('这个关键词确实没搜到，换个词试试')
    }
  } catch {
    message.error('搜索请求失败，检查后端是否在运行')
  } finally {
    sLoading.value = false
  }
}

async function doImport(c: AssetCandidate) {
  if (importGate.blocked.value) {
    message.warning(importGate.reason.value)
    return
  }
  sImporting.value = c.source_id
  try {
    const r = await importCandidate(c.source_name, c.source_id)
    message.success(r.created ? '已导入素材库' : '库里已有这张')
    await refreshAll()
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    const detailMsg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    if (status === 429) message.warning(detailMsg || '导入太频繁，稍后再试')
    else message.error(detailMsg || '导入失败')
  } finally {
    sImporting.value = ''
  }
}

// ---------------- 展示辅助 ----------------
function licenseTag(a: { source_name: string; license: string }) {
  if (a.source_name === 'pixabay') return { text: 'Pixabay', type: 'success' as const }
  if (a.source_name === 'openverse') {
    return a.license === 'cc0' || a.license === 'pdm'
      ? { text: 'CC0', type: 'success' as const }
      : { text: 'CC 需署名', type: 'warning' as const }
  }
  return { text: '自有', type: 'default' as const }
}

function sizeLabel(n: number): string {
  if (!n) return ''
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)}MB` : `${Math.round(n / 1024)}KB`
}

function dateLabel(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

// ---------------- 平铺列表列定义 ----------------
const flatColumns = computed<DataTableColumns<Asset>>(() => [
  { type: 'selection' },
  {
    title: '',
    key: 'thumb',
    width: 64,
    render: (row) =>
      h('img', {
        src: row.thumb_url,
        alt: row.title,
        style: 'width:48px;height:36px;object-fit:cover;border-radius:4px;display:block',
      }),
  },
  { title: '标题', key: 'title', ellipsis: { tooltip: true } },
  {
    title: '尺寸',
    key: 'wh',
    width: 90,
    render: (row) => `${row.width}×${row.height}`,
  },
  {
    title: '大小',
    key: 'size',
    width: 80,
    render: (row) => sizeLabel(row.size_bytes),
  },
  {
    title: '来源/许可',
    key: 'lic',
    width: 110,
    render: (row) => {
      const t = licenseTag(row)
      return h(NTag, { size: 'tiny', bordered: false, type: t.type }, { default: () => t.text })
    },
  },
  {
    title: '引用',
    key: 'used',
    width: 60,
    render: (row) => (row.used_count ? String(row.used_count) : '-'),
  },
  {
    title: '日期',
    key: 'date',
    width: 60,
    render: (row) => dateLabel(row.created_at),
  },
  {
    title: '',
    key: 'act',
    width: 70,
    render: (row) =>
      h(
        NButton,
        { size: 'tiny', quaternary: true, onClick: () => openDetail(row) },
        { default: () => '详情' },
      ),
  },
])

// ---------------- 树形列表（文件夹+素材混排） ----------------
interface TreeRow {
  key: string
  type: 'folder' | 'asset'
  name: string
  asset?: Asset
  count?: number
  children?: TreeRow[]
}

function buildTreeRows(): TreeRow[] {
  const rows: TreeRow[] = []
  const buildFolder = (n: AssetTreeNode): TreeRow => ({
    key: `folder:${n.id}`,
    type: 'folder',
    name: `${n.name}（${n.asset_count ?? 0}）`,
    count: n.asset_count ?? 0,
    children: [
      ...n.children.map(buildFolder),
      ...n.assets.map(
        (a): TreeRow => ({ key: `asset:${a.id}`, type: 'asset', name: a.title, asset: a }),
      ),
    ],
  })
  for (const n of treeNodes.value) rows.push(buildFolder(n))
  if (treeUncategorized.value.length) {
    rows.push({
      key: 'folder:__uncat__',
      type: 'folder',
      name: `未归类（${treeUncategorized.value.length}）`,
      count: treeUncategorized.value.length,
      children: treeUncategorized.value.map(
        (a): TreeRow => ({ key: `asset:${a.id}`, type: 'asset', name: a.title, asset: a }),
      ),
    })
  }
  return rows
}

const treeRows = computed(() => buildTreeRows())

const treeTableColumns = computed<DataTableColumns<TreeRow>>(() => [
  {
    // 只有素材行可勾选：文件夹行是结构节点，勾选它「批量删除」会语义不明
    type: 'selection',
    disabled: (row: TreeRow) => row.type !== 'asset',
  },
  {
    title: '名称',
    key: 'name',
    ellipsis: { tooltip: true },
    render: (row) => {
      if (row.type === 'folder') {
        return h('span', { style: 'font-weight:600' }, row.name)
      }
      return h('span', null, row.name || '未命名素材')
    },
  },
  {
    title: '缩略图',
    key: 'thumb',
    width: 64,
    render: (row) =>
      row.asset
        ? h('img', {
            src: row.asset.thumb_url,
            alt: row.asset.title,
            style: 'width:48px;height:36px;object-fit:cover;border-radius:4px;display:block',
          })
        : '-',
  },
  {
    title: '尺寸',
    key: 'wh',
    width: 90,
    render: (row) => (row.asset ? `${row.asset.width}×${row.asset.height}` : ''),
  },
  {
    title: '大小',
    key: 'size',
    width: 80,
    render: (row) => (row.asset ? sizeLabel(row.asset.size_bytes) : ''),
  },
  {
    title: '许可',
    key: 'lic',
    width: 110,
    render: (row) => {
      if (!row.asset) return ''
      const t = licenseTag(row.asset)
      return h(NTag, { size: 'tiny', bordered: false, type: t.type }, { default: () => t.text })
    },
  },
  {
    title: '日期',
    key: 'date',
    width: 60,
    render: (row) => (row.asset ? dateLabel(row.asset.created_at) : ''),
  },
  {
    title: '',
    key: 'act',
    width: 70,
    render: (row) =>
      row.asset
        ? h(
            NButton,
            { size: 'tiny', quaternary: true, onClick: () => openDetail(row.asset!) },
            { default: () => '详情' },
          )
        : null,
  },
])

onMounted(() => {
  void refreshAll()
  void loadSources()
  if (viewMode.value === 'tree') void loadTree()
})
</script>

<template>
  <div class="assets-page">
    <!-- 左侧：目录树 -->
    <NCard class="folder-panel" size="small">
      <template #header>
        <div class="fp-hd">
          <span>目录</span>
          <NButton size="tiny" quaternary @click="openCreateFolder(null)">+ 新建</NButton>
        </div>
      </template>
      <NTree
        v-if="treeData.length"
        :data="treeData"
        block-line
        :selected-keys="[folderSel]"
        :default-expanded-keys="['all', 'none']"
        @update:selected-keys="onTreeSelect"
      />
      <NEmpty v-else description="暂无目录" size="small" style="padding: 16px 0" />
      <!-- 右键/悬停操作：用 NDropdown，选中目录后点这里的按钮 -->
      <div v-if="folderSel.startsWith('f:')" class="fp-actions">
        <NDropdown
          trigger="click"
          :options="folderMenuOptions"
          @select="(k: string) => onFolderMenu(k, folders.find((f) => `f:${f.id}` === folderSel)! )"
        >
          <NButton size="tiny" secondary>目录操作</NButton>
        </NDropdown>
      </div>
    </NCard>

    <!-- 右侧：主内容 -->
    <NCard class="main-panel">
      <template #header>
        <div class="hd">
          <div class="hd-left">
            <NSelect
              v-model:value="kindFilter"
              :options="kindOptions"
              size="small"
              style="width: 112px"
              @update:value="onSearch"
            />
            <NSelect
              v-model:value="sourceFilter"
              :options="sourceOptions"
              size="small"
              style="width: 112px"
              @update:value="onSearch"
            />
            <NInput
              v-model:value="keyword"
              size="small"
              clearable
              placeholder="搜索标题或标签"
              style="width: 200px"
              @keyup.enter="onSearch"
              @clear="onSearch"
            />
            <NButton size="small" secondary @click="onSearch">搜索</NButton>
          </div>
          <div class="hd-right">
            <NRadioGroup :value="viewMode" size="small" @update:value="onViewModeChange">
              <NRadioButton value="grid">网格</NRadioButton>
              <NRadioButton value="flat">平铺</NRadioButton>
              <NRadioButton value="tree">树形</NRadioButton>
            </NRadioGroup>
            <NButton size="small" secondary @click="searchShow = true">在线搜图</NButton>
            <NUpload
              :show-file-list="false"
              accept="image/*"
              :custom-request="doUploadBatch"
              :disabled="uploading"
            >
              <NButton size="small" type="primary" :loading="uploading">上传图片</NButton>
            </NUpload>
          </div>
        </div>
      </template>

      <!-- 容量条：素材库最容易被忽略的成本是磁盘，让它一直可见 -->
      <div v-if="stats" class="usage">
        <span class="usage-label">已用 {{ usageText }}</span>
        <div class="usage-bar">
          <div class="usage-fill" :class="{ full: usagePct >= 90 }" :style="{ width: `${usagePct}%` }" />
        </div>
        <span class="usage-sub">共 {{ stats.total }} 个素材</span>
      </div>

      <!-- 批量工具条：选中 > 0 才出现 -->
      <div v-if="selectedCount > 0" class="bulk-bar">
        <span>已选 {{ selectedCount }} 个</span>
        <NButton size="tiny" secondary @click="selectAllPage">全选本页</NButton>
        <NButton size="tiny" quaternary @click="clearSelect">取消选择</NButton>
        <div class="bulk-gap" />
        <NButton size="tiny" secondary @click="doBatchDownload">打包下载</NButton>
        <NButton size="tiny" secondary @click="moveModalShow = true">移动到目录</NButton>
        <NButton
          size="tiny"
          type="error"
          secondary
          :disabled="deleteGate.blocked.value"
          @click="doBatchDelete"
        >
          批量删除
        </NButton>
      </div>

      <!-- ===== 视图一：网格 ===== -->
      <template v-if="viewMode === 'grid'">
        <NSpin :show="loading">
          <div v-if="items.length" class="grid">
            <div v-for="a in items" :key="a.id" class="cell" :class="{ checked: selectedIds.has(a.id) }">
              <button class="cell-click" type="button" @click="openDetail(a)">
                <img :src="a.thumb_url" :alt="a.title" loading="lazy" />
                <NTag class="badge" size="tiny" :bordered="false" :type="licenseTag(a).type">
                  {{ licenseTag(a).text }}
                </NTag>
                <div class="cell-body">
                  <div class="cell-title">{{ a.title || '未命名素材' }}</div>
                  <div class="cell-meta">
                    <span>{{ a.width }}×{{ a.height }}</span>
                    <span>{{ sizeLabel(a.size_bytes) }}</span>
                    <span v-if="a.used_count">用 {{ a.used_count }} 次</span>
                    <span class="cell-date">{{ dateLabel(a.created_at) }}</span>
                  </div>
                </div>
              </button>
              <NCheckbox
                class="cell-check"
                :checked="selectedIds.has(a.id)"
                @click.stop
                @update:checked="() => toggleSelect(a.id)"
              />
            </div>
          </div>
          <NEmpty
            v-else
            description="还没有素材。上传几张，或用「在线搜图」找免费商用素材"
            style="padding: 64px 0"
          />
        </NSpin>
      </template>

      <!-- ===== 视图二：平铺列表 ===== -->
      <template v-else-if="viewMode === 'flat'">
        <NDataTable
          :columns="flatColumns"
          :data="items"
          :row-key="(r: Asset) => r.id"
          :checked-row-keys="checkedKeys"
          :loading="loading"
          size="small"
          :bordered="false"
          @update:checked-row-keys="onCheckedRowKeysChange"
        />
      </template>

      <!-- ===== 视图三：树形（文件夹+素材混排） ===== -->
      <template v-else>
        <NSpin :show="treeLoading">
          <NDataTable
            v-if="treeRows.length"
            :columns="treeTableColumns"
            :data="treeRows"
            :row-key="(r: TreeRow) => r.key"
            :checked-row-keys="checkedKeys"
            :default-expand-all="true"
            size="small"
            :bordered="false"
            @update:checked-row-keys="onCheckedRowKeysChange"
          />
          <NEmpty v-else description="还没有素材，先建目录或上传图片" style="padding: 64px 0" />
        </NSpin>
      </template>

      <!-- 分页：网格/平铺用；树形是一次性全量，不需要 -->
      <div v-if="viewMode !== 'tree' && total > pageSize" class="pager">
        <NPagination
          v-model:page="page"
          :item-count="total"
          :page-size="pageSize"
          size="small"
          @update:page="reload"
        />
      </div>

      <!-- ---------------- 目录新建/重命名 ---------------- -->
      <NModal
        v-model:show="folderModalShow"
        preset="card"
        :title="folderModalMode === 'create' ? '新建目录' : '重命名目录'"
        style="width: 400px; max-width: 94vw"
      >
        <NInput
          v-model:value="folderName"
          size="small"
          placeholder="目录名"
          @keyup.enter="saveFolder"
        />
        <template #footer>
          <div class="detail-footer">
            <div class="footer-gap" />
            <NButton size="small" @click="folderModalShow = false">取消</NButton>
            <NButton size="small" type="primary" @click="saveFolder">确定</NButton>
          </div>
        </template>
      </NModal>

      <!-- ---------------- 移动到目录 ---------------- -->
      <NModal
        v-model:show="moveModalShow"
        preset="card"
        title="移动到目录"
        style="width: 400px; max-width: 94vw"
      >
        <NSelect
          v-model:value="moveTarget"
          :options="moveOptions"
          size="small"
          placeholder="选择目标目录"
        />
        <template #footer>
          <div class="detail-footer">
            <div class="footer-gap" />
            <NButton size="small" @click="moveModalShow = false">取消</NButton>
            <NButton size="small" type="primary" :loading="moving" @click="doBatchMove">移动</NButton>
          </div>
        </template>
      </NModal>

      <!-- ---------------- 详情 / 编辑 ---------------- -->
      <NModal
        v-model:show="detailShow"
        preset="card"
        :title="detail?.title || '素材详情'"
        style="width: 640px; max-width: 94vw"
      >
        <div v-if="detail" class="detail">
          <div class="detail-preview">
            <img :src="detail.url" :alt="detail.title" />
          </div>
          <div class="detail-form">
            <label class="field-label">标题</label>
            <NInput v-model:value="editTitle" size="small" placeholder="给这张图起个名字" />
            <label class="field-label">标签</label>
            <NSelect
              v-model:value="editTags"
              :options="editTags.map((t) => ({ label: t, value: t }))"
              multiple
              filterable
              tag
              size="small"
              placeholder="回车添加，便于以后按标签找图"
            />
            <div class="detail-meta">
              <div><span>尺寸</span>{{ detail.width }}×{{ detail.height }}</div>
              <div><span>大小</span>{{ sizeLabel(detail.size_bytes) }}</div>
              <div><span>来源</span>{{ detail.source_name || '手动上传' }}</div>
              <div v-if="detail.author"><span>作者</span>{{ detail.author }}</div>
              <div v-if="detail.attribution_text"><span>署名</span>{{ detail.attribution_text }}</div>
              <div v-if="detail.used_count"><span>被引用</span>{{ detail.used_count }} 次</div>
            </div>
            <div v-if="detail.source_url" class="detail-link">
              <a :href="detail.source_url" target="_blank" rel="noopener noreferrer">查看原页面</a>
              <span v-if="detail.license" class="license-note">{{ detail.license }}</span>
            </div>
          </div>
        </div>
        <template #footer>
          <div class="detail-footer">
            <NButton
              size="small"
              type="error"
              secondary
              :disabled="deleteGate.blocked.value"
              @click="detail && removeAsset(detail)"
            >
              删除
            </NButton>
            <div class="footer-gap" />
            <NButton size="small" @click="detailShow = false">关闭</NButton>
            <NButton size="small" type="primary" :loading="saving" @click="saveDetail">保存</NButton>
          </div>
        </template>
      </NModal>

      <!-- ---------------- 在线搜图 ---------------- -->
      <NModal
        v-model:show="searchShow"
        preset="card"
        title="在线搜图"
        style="width: 820px; max-width: 94vw"
      >
        <div class="search-bar">
          <NInput
            v-model:value="sq"
            size="small"
            placeholder="英文关键词效果更好，如 office desk"
            style="width: 300px"
            @keyup.enter="doSearch"
          />
          <NButton size="small" type="primary" :loading="sLoading" @click="doSearch">搜索</NButton>
          <NTag
            v-if="sources.length && !sources.find((s) => s.name === 'pixabay')?.configured"
            size="tiny"
            :bordered="false"
            type="warning"
          >
            Pixabay 未配置，当前只有 Openverse
          </NTag>
        </div>
        <div v-if="sErrors.length" class="search-err">
          <div v-for="e in sErrors" :key="e.source">
            <strong>{{ e.source === 'pixabay' ? 'Pixabay' : 'Openverse' }}</strong>：{{ e.reason }}
          </div>
          <div class="search-err-tip">
            两个源都不可用时搜不到任何图。Pixabay 免费 Key 在
            <a href="https://pixabay.com/api/docs/" target="_blank" rel="noreferrer">pixabay.com/api/docs</a>
            申请后填进 <code>backend/.env</code> 的 <code>PIXABAY_API_KEY</code>，重启后端即可。
          </div>
        </div>
        <div class="search-note">
          只显示允许商用的素材。点「导入」才下载入库，不做批量抓取 —— 这是图源条款的限制，也是我们不替你囤图的理由。
        </div>
        <NSpin :show="sLoading">
          <div class="search-pane">
            <div v-if="sItems.length" class="grid">
              <div v-for="c in sItems" :key="`${c.source_name}-${c.source_id}`" class="cell s-cell">
                <img :src="c.thumb_url" :alt="c.title" loading="lazy" />
                <NTag class="badge" size="tiny" :bordered="false" :type="licenseTag(c).type">
                  {{ licenseTag(c).text }}
                </NTag>
                <div class="cell-body">
                  <div class="cell-title">{{ c.title || '未命名' }}</div>
                  <div class="cell-meta">
                    <span v-if="c.author">{{ c.author }}</span>
                  </div>
                </div>
                <NButton
                  class="s-import"
                  size="tiny"
                  type="primary"
                  :loading="sImporting === c.source_id"
                  :disabled="importGate.blocked.value"
                  @click="doImport(c)"
                >
                  导入
                </NButton>
              </div>
            </div>
            <NEmpty
              v-else
              :description="
                sErrors.length
                  ? '图源当前不可用，具体原因见上方'
                  : searched
                    ? '没搜到，换个关键词试试'
                    : '输入关键词搜索免费商用素材'
              "
              style="padding: 56px 0"
            />
          </div>
        </NSpin>
      </NModal>
    </NCard>
  </div>
</template>

<style scoped>
.assets-page {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 12px;
  align-items: start;
}
.folder-panel {
  position: sticky;
  top: 12px;
}
.fp-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.fp-actions {
  margin-top: 10px;
}
.main-panel {
  min-width: 0;
}
.hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.hd-left,
.hd-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* NUpload 渲染出的是块级 .n-upload（宽度 100%），作为 flex 子项会把
   「上传图片」挤到下一行，看起来像两个按钮上下错位。这里收成行内块，
   宽度交给内部按钮自己撑。 */
.hd-right :deep(.n-upload) {
  display: inline-flex;
  width: auto;
}
.usage {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--app-text-3, #888);
}
.usage-bar {
  flex: 1;
  max-width: 260px;
  height: 6px;
  border-radius: 3px;
  background: var(--app-sunken, rgba(128, 128, 128, 0.15));
  overflow: hidden;
}
.usage-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--app-primary, #2080f0);
  transition: width 0.2s;
}
.usage-fill.full {
  background: var(--app-red, #d03050);
}
.bulk-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--app-sunken, rgba(128, 128, 128, 0.08));
  font-size: 12px;
}
.bulk-gap {
  flex: 1;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 12px;
}
.cell {
  position: relative;
}
.cell-click {
  display: flex;
  flex-direction: column;
  width: 100%;
  padding: 0;
  border: 1px solid var(--app-border, rgba(128, 128, 128, 0.18));
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-sunken, rgba(128, 128, 128, 0.04));
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, transform 0.15s;
}
.cell-click:hover {
  border-color: var(--app-primary, #2080f0);
  transform: translateY(-1px);
}
.cell.checked .cell-click {
  border-color: var(--app-primary, #2080f0);
}
.cell-click img {
  display: block;
  width: 100%;
  height: 118px;
  object-fit: cover;
  background: rgba(128, 128, 128, 0.12);
}
.cell-check {
  position: absolute;
  top: 6px;
  right: 6px;
}
.badge {
  position: absolute;
  top: 6px;
  left: 6px;
}
.cell-body {
  padding: 6px 8px 8px;
}
.cell-title {
  font-size: 12.5px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cell-meta {
  display: flex;
  gap: 8px;
  margin-top: 3px;
  font-size: 11px;
  color: var(--app-text-3, #999);
  overflow: hidden;
  white-space: nowrap;
}
.cell-date {
  margin-left: auto;
}
.pager {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}
.detail {
  display: flex;
  gap: 16px;
}
.detail-preview {
  flex: 0 0 240px;
  border-radius: 8px;
  overflow: hidden;
  background: rgba(128, 128, 128, 0.1);
  display: flex;
  align-items: center;
}
.detail-preview img {
  width: 100%;
  max-height: 300px;
  object-fit: contain;
}
.detail-form {
  flex: 1;
  min-width: 0;
}
.field-label {
  display: block;
  margin: 0 0 4px;
  font-size: 12px;
  color: var(--app-text-3, #888);
}
.field-label:not(:first-child) {
  margin-top: 12px;
}
.detail-meta {
  margin-top: 14px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  font-size: 12px;
  color: var(--app-text-2, #666);
}
.detail-meta span {
  display: inline-block;
  min-width: 40px;
  color: var(--app-text-3, #999);
}
.detail-link {
  margin-top: 12px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.license-note {
  color: var(--app-text-3, #999);
}
.detail-footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.footer-gap {
  flex: 1;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.search-note {
  margin: 10px 0;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--app-text-3, #888);
  background: var(--app-sunken, rgba(128, 128, 128, 0.06));
}
.search-err {
  margin: 10px 0;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-red, #d03050);
  background: rgba(208, 48, 80, 0.08);
  border: 1px solid rgba(208, 48, 80, 0.22);
}
.search-err-tip {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed rgba(208, 48, 80, 0.22);
  color: var(--app-text-2, #666);
}
.search-err-tip a {
  color: var(--app-primary, #2080f0);
}
.search-err-tip code {
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--app-sunken, rgba(128, 128, 128, 0.14));
}
.search-pane {
  max-height: 58vh;
  overflow-y: auto;
}
.s-cell {
  cursor: default;
}
.s-import {
  margin: 0 8px 8px;
}

/* 窄屏：目录栏收到顶部 */
@media (max-width: 860px) {
  .assets-page {
    grid-template-columns: 1fr;
  }
  .folder-panel {
    position: static;
  }
}
</style>
