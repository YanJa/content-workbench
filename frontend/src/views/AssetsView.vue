<script setup lang="ts">
/**
 * 素材库页面。
 *
 * 页面职责与「选取弹窗」分开：这里是**管理**（看全部、改标签、删、看授权信息），
 * 弹窗是**取用**（写文章时挑一张插进去）。同一个后端，两种用法。
 *
 * 一条合规设计：卡片上始终显示许可徽标。素材库最容易出的事不是技术问题，
 * 是「这张图能不能商用」记不清了 —— 让它在界面上一直可见，比藏在详情里可靠。
 */
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NEmpty,
  NInput,
  NModal,
  NPagination,
  NSelect,
  NSpin,
  NTag,
  NUpload,
  useDialog,
  useMessage,
  type UploadCustomRequestOptions,
} from 'naive-ui'

import {
  assetStats,
  deleteAsset,
  importCandidate,
  listAssetSources,
  listAssets,
  searchOnline,
  updateAsset,
  uploadAsset,
  type Asset,
  type AssetCandidate,
  type AssetSourceStatus,
  type AssetStats,
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
  await Promise.all([reload(), loadStats()])
}

function onSearch() {
  page.value = 1
  void reload()
}

async function doUpload({ file, onFinish, onError }: UploadCustomRequestOptions) {
  const f = file.file
  if (!f) {
    onError()
    return
  }
  uploading.value = true
  try {
    const r = await uploadAsset(f)
    message.success(r.created ? '已上传' : '这张图库里已经有，未重复保存')
    onFinish()
    page.value = 1
    await refreshAll()
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

onMounted(() => {
  void refreshAll()
  void loadSources()
})
</script>

<template>
  <NCard>
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
          <NButton size="small" secondary @click="searchShow = true">在线搜图</NButton>
          <NUpload
            :show-file-list="false"
            accept="image/*"
            :custom-request="doUpload"
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

    <NSpin :show="loading">
      <div v-if="items.length" class="grid">
        <button v-for="a in items" :key="a.id" class="cell" type="button" @click="openDetail(a)">
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
      </div>
      <NEmpty
        v-else
        description="还没有素材。上传几张，或用「在线搜图」找免费商用素材"
        style="padding: 64px 0"
      />
    </NSpin>

    <div v-if="total > pageSize" class="pager">
      <NPagination
        v-model:page="page"
        :item-count="total"
        :page-size="pageSize"
        size="small"
        @update:page="reload"
      />
    </div>

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
</template>

<style scoped>
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
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 12px;
}
.cell {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 0;
  border: 1px solid var(--app-border, rgba(128, 128, 128, 0.18));
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-sunken, rgba(128, 128, 128, 0.04));
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, transform 0.15s;
}
.cell:hover {
  border-color: var(--app-primary, #2080f0);
  transform: translateY(-1px);
}
.cell img {
  display: block;
  width: 100%;
  height: 118px;
  object-fit: cover;
  background: rgba(128, 128, 128, 0.12);
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
</style>
