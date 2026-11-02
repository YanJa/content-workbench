<script setup lang="ts">
/**
 * 素材库选取弹窗。
 *
 * 三个设计决定：
 * 1. **点一下即插入，但不关闭弹窗**。写文章时常常要连着插两三张，
 *    每插一张就关窗再开一遍是纯打扰。已插入的卡片打勾，避免重复插同一张。
 * 2. **在线搜图只把候选摆出来，选中才下载入库**。既符合 Pixabay
 *    「别批量下载」的条款，也不会让用户随手一搜就往库里塞几十张。
 * 3. **导入只传 source_name + source_id**。下载地址由后端回源站查，
 *    前端不构造 URL —— 否则这个接口就是 SSRF 跳板（见 routers/assets.py 头注释）。
 */
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NEmpty,
  NInput,
  NPagination,
  NSpin,
  NTabPane,
  NTabs,
  NTag,
  NUpload,
  useMessage,
  type UploadCustomRequestOptions,
} from 'naive-ui'

import {
  importCandidate,
  listAssetSources,
  listAssets,
  searchOnline,
  uploadAsset,
  type Asset,
  type AssetCandidate,
  type AssetSourceStatus,
} from '@/api/assets'
import { usePermGate } from '@/composables/usePermGate'

const props = defineProps<{
  show: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'picked', a: Asset): void
}>()

const message = useMessage()
const importGate = usePermGate({ perm: 'assets.import' })

const tab = ref<'local' | 'online'>('local')

// ---------------- 我的素材 ----------------
const items = ref<Asset[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 24
const loading = ref(false)
const keyword = ref('')

// ---------------- 在线搜图 ----------------
const sources = ref<AssetSourceStatus[]>([])
const onlineQ = ref('')
const onlineItems = ref<AssetCandidate[]>([])
const onlineLoading = ref(false)
const importingId = ref('')
const onlineSearched = ref(false)
/** 各图源的失败原因。空数组 = 没有源失败。 */
const searchErrors = ref<{ source: string; reason: string }[]>([])

const uploading = ref(false)
/** 本次已插入的素材 id，用于卡片打勾 */
const inserted = ref<Set<string>>(new Set())

const visible = computed({
  get: () => props.show,
  set: (v: boolean) => emit('update:show', v),
})

const pixabayReady = computed(
  () => sources.value.find((s) => s.name === 'pixabay')?.configured ?? false,
)

async function loadLocal() {
  loading.value = true
  try {
    const r = await listAssets({ q: keyword.value.trim() || undefined, page: page.value, page_size: pageSize })
    items.value = r.items
    total.value = r.total
  } catch {
    message.error('素材列表加载失败')
  } finally {
    loading.value = false
  }
}

async function loadSources() {
  try {
    sources.value = (await listAssetSources()).sources
  } catch {
    sources.value = []
  }
}

function onSearch() {
  page.value = 1
  void loadLocal()
}

/**
 * 用 custom-request 而不是 @change：后者在文件状态每次变化（选中→上传中→完成）
 * 都会触发一遍，一次上传会被调用三四次。
 */
async function onUpload({ file, onFinish, onError }: UploadCustomRequestOptions) {
  const f = file.file
  if (!f) {
    onError()
    return
  }
  uploading.value = true
  try {
    const r = await uploadAsset(f)
    message.success(r.created ? '已上传到素材库' : '这张图库里已经有了，直接用即可')
    onFinish()
    page.value = 1
    await loadLocal()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail || '上传失败')
    onError()
  } finally {
    uploading.value = false
  }
}

async function onOnlineSearch() {
  const q = onlineQ.value.trim()
  if (!q) {
    message.warning('输入关键词再搜')
    return
  }
  onlineLoading.value = true
  onlineSearched.value = true
  searchErrors.value = []
  try {
    const r = await searchOnline({ q, source: 'all', per_page: 24 })
    onlineItems.value = r.items
    sources.value = r.sources
    searchErrors.value = r.errors || []
    if (r.items.length) return
    // 空结果分两种，对用户的意义完全不同：源挂了 vs 真没图。
    // 说错会让人一直换关键词白试。
    if (searchErrors.value.length) {
      // 面板里已有详细错误卡，这里不再弹第二遍
    } else if (!sources.value.some((s) => s.configured)) {
      message.warning('还没有可用的图源，先配一个 API Key')
    } else {
      message.info('这个关键词确实没搜到，换个词试试')
    }
  } catch {
    message.error('搜索请求失败，检查后端是否在运行')
  } finally {
    onlineLoading.value = false
  }
}

/** 选中一张。**不在这里调插入接口** —— 弹窗只管选，落地由调用方决定
 *  （配图面板要插进 content.images，将来别处可能只是取个链接）。 */
function pick(a: Asset) {
  if (inserted.value.has(a.id)) return
  inserted.value.add(a.id)
  // Set 是引用类型，重新赋值才能触发视图更新
  inserted.value = new Set(inserted.value)
  emit('picked', a)
}

/** 在线候选：先导入入库，再走 insert */
async function importAndPick(c: AssetCandidate) {
  if (importGate.blocked.value) {
    message.warning(importGate.reason.value)
    return
  }
  importingId.value = c.source_id
  try {
    const r = await importCandidate(c.source_name, c.source_id)
    message.success(r.created ? '已导入素材库，已选中' : '素材库里已有这张，已选中')
    pick(r.asset)
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    if (status === 429) message.warning(detail || '导入太频繁，稍后再试')
    else message.error(detail || '导入失败')
  } finally {
    importingId.value = ''
  }
}

function sizeLabel(n: number): string {
  if (!n) return ''
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)}MB` : `${Math.round(n / 1024)}KB`
}

/** 来源徽标：告诉用户「这张能不能商用、要不要署名」 */
function licenseTag(a: { source_name: string; license: string }): { text: string; type: 'default' | 'success' | 'warning' } {
  if (a.source_name === 'pixabay') return { text: 'Pixabay · 可商用', type: 'success' }
  if (a.source_name === 'openverse') {
    return a.license === 'cc0' || a.license === 'pdm'
      ? { text: 'CC0 · 免署名', type: 'success' }
      : { text: 'CC · 需署名', type: 'warning' }
  }
  return { text: '自有', type: 'default' }
}

watch(
  () => props.show,
  (v) => {
    if (!v) {
      inserted.value = new Set()
      return
    }
    inserted.value = new Set()
    page.value = 1
    keyword.value = ''
    onlineQ.value = ''
    onlineItems.value = []
    onlineSearched.value = false
    void loadLocal()
    void loadSources()
  },
)

watch(page, () => void loadLocal())
</script>

<template>
  <NModal
    v-model:show="visible"
    preset="card"
    title="素材库"
    style="width: 880px; max-width: 94vw"
    :bordered="false"
  >
    <NTabs v-model:value="tab" type="line" animated>
      <!-- ---------------- 我的素材 ---------------- -->
      <NTabPane name="local" :tab="`我的素材${total ? `（${total}）` : ''}`">
        <div class="bar">
          <NInput
            v-model:value="keyword"
            placeholder="搜索标题或标签"
            clearable
            size="small"
            style="width: 220px"
            @keyup.enter="onSearch"
          />
          <NButton size="small" secondary @click="onSearch">搜索</NButton>
          <div class="bar-gap" />
          <NUpload
            :show-file-list="false"
            accept="image/*"
            :disabled="uploading"
            :custom-request="onUpload"
          >
            <NButton size="small" type="primary" :loading="uploading">上传图片</NButton>
          </NUpload>
        </div>

        <div class="pane">
          <NSpin :show="loading">
            <div v-if="items.length" class="grid">
              <button
                v-for="a in items"
                :key="a.id"
                class="cell"
                :class="{ done: inserted.has(a.id) }"
                type="button"
                @click="pick(a)"
              >
                <img :src="a.thumb_url" :alt="a.title" loading="lazy" />
                <span v-if="inserted.has(a.id)" class="done-mark">✓ 已插入</span>
                <span class="cell-meta">{{ sizeLabel(a.size_bytes) }}</span>
                <span class="cell-title">{{ a.title || '未命名' }}</span>
              </button>
            </div>
            <NEmpty v-else description="还没有素材，先上传几张，或用「在线搜图」找" style="padding: 48px 0" />
          </NSpin>
        </div>

        <div v-if="total > pageSize" class="pager">
          <NPagination v-model:page="page" :item-count="total" :page-size="pageSize" size="small" />
        </div>
      </NTabPane>

      <!-- ---------------- 在线搜图 ---------------- -->
      <NTabPane name="online" tab="在线搜图">
        <div class="bar">
          <NInput
            v-model:value="onlineQ"
            placeholder="英文关键词效果更好，如 coffee shop"
            clearable
            size="small"
            style="width: 300px"
            @keyup.enter="onOnlineSearch"
          />
          <NButton size="small" type="primary" :loading="onlineLoading" @click="onOnlineSearch">
            搜索
          </NButton>
          <NTag v-if="!pixabayReady" size="tiny" :bordered="false" type="warning">
            Pixabay 未配置，当前只有 Openverse
          </NTag>
        </div>

        <!-- 图源失败要说清楚：否则「连不上」会被当成「没这个关键词」 -->
        <div v-if="searchErrors.length" class="err-box">
          <div v-for="e in searchErrors" :key="e.source" class="err-row">
            <strong>{{ e.source === 'pixabay' ? 'Pixabay' : 'Openverse' }}</strong>：{{ e.reason }}
          </div>
          <div class="err-tip">
            两个源都不可用时搜不到任何图。Pixabay 免费 Key 在
            <a href="https://pixabay.com/api/docs/" target="_blank" rel="noreferrer">pixabay.com/api/docs</a>
            申请后填进 <code>backend/.env</code> 的 <code>PIXABAY_API_KEY</code>，重启后端即可。
          </div>
        </div>

        <div class="notice">
          搜到的都是「允许商用」的素材（Pixabay 内容许可 / CC 商用授权）。点一张才下载入库，
          不会批量抓取。带「需署名」标记的，用到文章里请保留作者信息。
        </div>

        <div class="pane">
          <NSpin :show="onlineLoading">
            <div v-if="onlineItems.length" class="grid">
              <button
                v-for="c in onlineItems"
                :key="`${c.source_name}-${c.source_id}`"
                class="cell"
                type="button"
                :disabled="importingId === c.source_id"
                @click="importAndPick(c)"
              >
                <img :src="c.thumb_url" :alt="c.title" loading="lazy" />
                <span v-if="importingId === c.source_id" class="done-mark">导入中…</span>
                <NTag class="cell-badge" size="tiny" :bordered="false" :type="licenseTag(c).type">
                  {{ licenseTag(c).text }}
                </NTag>
                <span class="cell-title">{{ c.title || '未命名' }}</span>
                <span v-if="c.author" class="cell-author">{{ c.author }}</span>
              </button>
            </div>
            <NEmpty
              v-else
              :description="
                searchErrors.length
                  ? '图源当前不可用，具体原因见上方'
                  : onlineSearched
                    ? '没搜到，换个关键词试试'
                    : '输入关键词搜索免费商用素材'
              "
              style="padding: 48px 0"
            />
          </NSpin>
        </div>
      </NTabPane>
    </NTabs>
  </NModal>
</template>

<style scoped>
.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 12px;
  flex-wrap: wrap;
}
.bar-gap {
  flex: 1;
}
/* 同 AssetsView：.n-upload 是块级且宽 100%，做 flex 子项会挤掉旁边的按钮 */
.bar :deep(.n-upload) {
  display: inline-flex;
  width: auto;
}
.notice {
  font-size: 12px;
  line-height: 1.6;
  color: var(--app-text-3, #888);
  background: var(--app-sunken, rgba(128, 128, 128, 0.06));
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 10px;
}
.err-box {
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-red, #d03050);
  background: rgba(208, 48, 80, 0.08);
  border: 1px solid rgba(208, 48, 80, 0.22);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 10px;
}
.err-row + .err-row {
  margin-top: 2px;
}
.err-tip {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed rgba(208, 48, 80, 0.22);
  color: var(--app-text-2, #666);
}
.err-tip a {
  color: var(--app-primary, #2080f0);
}
.err-tip code {
  font-size: 11px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--app-sunken, rgba(128, 128, 128, 0.14));
}
.pane {
  max-height: 52vh;
  overflow-y: auto;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}
.cell {
  position: relative;
  display: block;
  padding: 0;
  border: 1px solid var(--app-border, rgba(128, 128, 128, 0.18));
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-sunken, rgba(128, 128, 128, 0.05));
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, transform 0.15s;
}
.cell:hover {
  border-color: var(--app-primary, #2080f0);
  transform: translateY(-1px);
}
.cell.done {
  border-color: var(--app-green, #18a058);
}
.cell img {
  display: block;
  width: 100%;
  height: 108px;
  object-fit: cover;
  background: rgba(128, 128, 128, 0.12);
}
.cell-title {
  display: block;
  padding: 5px 7px 2px;
  font-size: 12px;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cell-author {
  display: block;
  padding: 0 7px 6px;
  font-size: 11px;
  color: var(--app-text-3, #999);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cell-meta {
  position: absolute;
  top: 5px;
  left: 5px;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
}
.cell-badge {
  position: absolute;
  top: 5px;
  left: 5px;
}
.done-mark {
  position: absolute;
  inset: 0 0 auto auto;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 0 0 0 6px;
  background: var(--app-green, #18a058);
  color: #fff;
}
.pager {
  display: flex;
  justify-content: center;
  margin-top: 12px;
}
</style>
