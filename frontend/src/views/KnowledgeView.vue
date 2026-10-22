<script setup lang="ts">
/**
 * 知识库页：素材的录入 / 列表 / 检索调试。
 *
 * 定位是「给 AI 用的素材库」，不是给人读的文库：存进去的东西会在写稿时
 * 按语义自动召回、注入模型上下文。所以这一页刻意做了两件反直觉的事：
 *   1. 列表页不展示正文（要看法正文点「查看」）—— 正文是给模型看的；
 *   2. 提供「召回调试」面板。用户最常问的是「为什么 AI 没用上我存的」，
 *      没有这个面板就只能靠猜。
 */
import { computed, h, onMounted, ref, watch, type VNode } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NEmpty,
  NInput,
  NModal,
  NPagination,
  NSelect,
  NSpace,
  NTag,
  NText,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'

import IconTextBtn from '@/components/IconTextBtn.vue'
import { actionBtn } from '@/utils/actionBtn'
import { usePermGate } from '@/composables/usePermGate'
import {
  KNOWLEDGE_SOURCE_LABEL,
  createKnowledge,
  deleteKnowledge,
  getKnowledge,
  knowledgeStats,
  listKnowledge,
  renameKnowledge,
  retryKnowledge,
  searchKnowledge,
  uploadKnowledge,
} from '@/api/knowledge'
import type { KnowledgeHit, KnowledgeItem, KnowledgeStats, KnowledgeStatus } from '@/types'

const message = useMessage()
const dialog = useDialog()

/** 录入与删除各自的门控：后端也是这两个权限点，口径一致 */
const createGate = usePermGate({ perm: 'knowledge.create' })
const deleteGate = usePermGate({ perm: 'knowledge.delete' })

const route = useRoute()
const router = useRouter()

const items = ref<KnowledgeItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const searchKw = ref('')
const sourceFilter = ref<string>('all')
const stats = ref<KnowledgeStats | null>(null)

const sourceOptions = [
  { label: '全部来源', value: 'all' },
  { label: '手动录入', value: 'manual' },
  { label: '来自成稿', value: 'content' },
  { label: '文档上传', value: 'upload' },
  { label: '网页抓取', value: 'url' },
]

const statusMeta: Record<
  KnowledgeStatus,
  { label: string; type: 'default' | 'info' | 'success' | 'warning' | 'error' }
> = {
  pending: { label: '排队中', type: 'default' },
  processing: { label: '向量化中', type: 'info' },
  ready: { label: '已就绪', type: 'success' },
  partial: { label: '部分失败', type: 'warning' },
  failed: { label: '失败', type: 'error' },
}

async function load() {
  loading.value = true
  try {
    const [p, s] = await Promise.all([
      listKnowledge({
        q: searchKw.value,
        source: sourceFilter.value,
        page: page.value,
        pageSize: pageSize.value,
      }),
      knowledgeStats(),
    ])
    items.value = p.items
    total.value = p.total
    stats.value = s
  } catch (e) {
    message.error((e as Error).message || '加载知识库失败')
  } finally {
    loading.value = false
  }
}

/** 300ms 防抖：搜索框每敲一个字就打一次后端太浪费 */
let timer: ReturnType<typeof setTimeout> | undefined
watch([searchKw, sourceFilter], () => {
  clearTimeout(timer)
  timer = setTimeout(() => {
    page.value = 1
    void load()
  }, 300)
})

// 新增弹窗
const formShow = ref(false)
const formTitle = ref('')
const formText = ref('')
const formFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const submitting = ref(false)

function openCreate() {
  formTitle.value = ''
  formText.value = ''
  formFile.value = null
  formShow.value = true
}

function onPickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (f) formFile.value = f
  // 允许再次选同一个文件
  input.value = ''
}

async function submitCreate() {
  // 文件优先：选了文件就走上传通道，文本忽略；都没填才报错
  if (formFile.value) {
    submitting.value = true
    try {
      const { already_exists } = await uploadKnowledge(formFile.value, formTitle.value || undefined)
      if (already_exists) {
        message.info('该文件已在知识库中')
      } else {
        message.success('已存入知识库，正在后台向量化')
      }
      formShow.value = false
      await load()
    } catch (e) {
      message.error((e as Error).message || '上传失败')
    } finally {
      submitting.value = false
    }
    return
  }
  if (!formText.value.trim()) {
    message.warning('请填写素材内容，或上传一个文档')
    return
  }
  submitting.value = true
  try {
    const { already_exists } = await createKnowledge({
      title: formTitle.value,
      text: formText.value,
    })
    if (already_exists) {
      message.info('该内容已在知识库中')
    } else {
      message.success('已存入知识库，正在后台向量化')
    }
    formShow.value = false
    await load()
  } catch (e) {
    message.error((e as Error).message || '存入失败')
  } finally {
    submitting.value = false
  }
}

// 详情弹窗
const detailShow = ref(false)
const detail = ref<KnowledgeItem | null>(null)

async function openDetail(row: KnowledgeItem) {
  // 列表接口不带正文与片段，详情单独拉（列表页不背正文是刻意的）
  try {
    detail.value = await getKnowledge(row.id)
  } catch (e) {
    message.error((e as Error).message || '加载详情失败')
    return
  }
  detailShow.value = true
}

// 重命名
const renameShow = ref(false)
const renameId = ref('')
const renameTitle = ref('')

function openRename(row: KnowledgeItem) {
  renameId.value = row.id
  renameTitle.value = row.title
  renameShow.value = true
}

async function submitRename() {
  try {
    await renameKnowledge(renameId.value, renameTitle.value)
    message.success('已重命名')
    renameShow.value = false
    await load()
  } catch (e) {
    message.error((e as Error).message || '重命名失败')
  }
}

async function onRetry(row: KnowledgeItem) {
  try {
    await retryKnowledge(row.id)
    message.success('已重新排队向量化')
    await load()
  } catch (e) {
    message.error((e as Error).message || '重试失败')
  }
}

function onDelete(row: KnowledgeItem) {
  dialog.warning({
    title: '删除素材',
    content: `确定删除「${row.title}」吗？它的 ${row.chunk_count} 个片段会一起删除，写稿时将不再召回这些内容。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteKnowledge(row.id)
        message.success('已删除')
        await load()
      } catch (e) {
        message.error((e as Error).message || '删除失败')
      }
    },
  })
}

const columns = computed<DataTableColumns<KnowledgeItem>>(() => [
  {
    title: '标题',
    key: 'title',
    minWidth: 220,
    render: (row) =>
      h('div', { style: 'display:flex;flex-direction:column;gap:2px' }, [
        h('span', { style: 'font-weight:500' }, row.title || '未命名素材'),
        row.error ? h('span', { style: 'font-size:12px;opacity:0.65' }, row.error) : null,
      ]),
  },
  { title: '来源', key: 'source', width: 100, render: (row) => KNOWLEDGE_SOURCE_LABEL[row.source] ?? row.source },
  {
    title: '规模',
    key: 'scale',
    width: 130,
    render: (row) => `${row.chunk_count} 片 / ${row.char_count} 字`,
  },
  {
    title: '状态',
    key: 'status',
    width: 100,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: statusMeta[row.status]?.type ?? 'default', bordered: false },
        { default: () => statusMeta[row.status]?.label ?? row.status },
      ),
  },
  {
    title: '存入时间',
    key: 'created_at',
    width: 120,
    render: (row) => new Date(row.created_at * 1000).toLocaleDateString('zh-CN'),
  },
  {
    title: '操作',
    key: 'actions',
    width: 200,
    render: (row) => {
      const list = [
        actionBtn('detail', '查看素材正文与切分结果', () => openDetail(row)),
        actionBtn('edit', '重命名', () => openRename(row), { disabled: deleteGate.blocked.value }),
        row.status === 'failed' || row.status === 'partial'
          ? actionBtn('reset', '重试向量化', () => onRetry(row), {
              type: 'warning',
              disabled: createGate.blocked.value,
            })
          : null,
        actionBtn('trash', '删除素材', () => onDelete(row), {
          type: 'error',
          disabled: deleteGate.blocked.value,
        }),
      ].filter(Boolean) as VNode[]
      return h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, list)
    },
  },
])

// ---------------------------------------------------------------- 召回调试
const probeQuery = ref('')
const probeHits = ref<KnowledgeHit[]>([])
const probing = ref(false)

async function runProbe() {
  if (!probeQuery.value.trim()) return
  probing.value = true
  try {
    probeHits.value = await searchKnowledge(probeQuery.value)
    if (!probeHits.value.length) message.info('没有命中任何素材（低于阈值）')
  } catch (e) {
    message.error((e as Error).message || '检索失败')
  } finally {
    probing.value = false
  }
}

onMounted(() => {
  void load()
  // 从发布队列「查看知识库」跳来时带 ?item=<id>，自动打开详情。
  const itemId = route.query.item as string | undefined
  if (itemId) void openDetail({ id: itemId } as KnowledgeItem)
})

/** 素材来自某篇成稿时，跳回发布队列并高亮那一行（双向闭环）。 */
function gotoSource() {
  if (detail.value?.source === 'content' && detail.value.origin_ref) {
    router.push({ path: '/queue', query: { content_id: detail.value.origin_ref } })
  }
}
</script>

<template>
  <NCard>
    <template #header>
      <div class="kb-header">
        <div class="kb-filters">
          <NSelect
            v-model:value="sourceFilter"
            :options="sourceOptions"
            size="small"
            style="width: 140px"
          />
          <NInput
            v-model:value="searchKw"
            size="small"
            clearable
            placeholder="搜索标题或原文"
            style="width: 200px"
          >
            <template #prefix>
              <span style="font-size: 13px; opacity: 0.6">🔍</span>
            </template>
          </NInput>
        </div>
        <NSpace :size="8">
          <NTag size="small" :bordered="false">
            {{ stats?.items ?? 0 }} / {{ stats?.items_limit ?? 0 }} 条
          </NTag>
          <NTag size="small" type="success" :bordered="false">
            可检索 {{ stats?.ready_chunks ?? 0 }} 片
          </NTag>
          <IconTextBtn
            name="plus"
            size="small"
            type="primary"
            :disabled="createGate.blocked.value"
            @click="openCreate"
          >新增素材</IconTextBtn>
          <IconTextBtn name="reset" size="small" @click="load">刷新</IconTextBtn>
        </NSpace>
      </div>
    </template>

    <NAlert v-if="stats && !stats.vector_ready" type="warning" :bordered="false" style="margin-bottom: 14px">
      <div style="font-size: 12.5px; line-height: 1.7">
        尚未配置向量化通道（Cloudflare 凭证），检索已退化为<b>关键词匹配</b>：
        素材照样能存、能命中，但只认字面对得上的词，同义改写会漏。
        在系统设置配好 <code>CLOUDFLARE_API_TOKEN</code> / <code>CLOUDFLARE_ACCOUNT_ID</code>
        后，新素材会自动用语义检索。
      </div>
    </NAlert>

    <div class="kb-probe">
      <NInput
        v-model:value="probeQuery"
        size="small"
        placeholder="召回调试：输入一句话，看看 AI 会用到哪些素材"
        style="flex: 1"
        @keyup.enter="runProbe"
      />
      <IconTextBtn name="search" size="small" :loading="probing" @click="runProbe">
        试试看
      </IconTextBtn>
    </div>
    <div v-if="probeHits.length" class="kb-probe-result">
      <div v-for="(hit, i) in probeHits" :key="i" class="kb-hit">
        <div class="kb-hit-head">
          <b>{{ hit.title }}</b>
          <NTag size="small" :bordered="false" :type="hit.match === 'vector' ? 'success' : 'warning'">
            {{ hit.match === 'vector' ? '语义' : '关键词' }} {{ hit.score.toFixed(2) }}
          </NTag>
        </div>
        <div class="kb-hit-body">{{ hit.content }}</div>
      </div>
    </div>

    <NEmpty
      v-if="!items.length && !loading"
      description="还没有素材。存进去的内容会在写稿时自动被 AI 参考。"
    />
    <template v-else>
      <NDataTable
        :columns="columns"
        :data="items"
        :loading="loading"
        :row-key="(r: KnowledgeItem) => r.id"
        :scroll-x="900"
        size="small"
      />
      <div class="kb-pager">
        <NPagination
          v-model:page="page"
          :page-size="pageSize"
          :item-count="total"
          :page-sizes="[10, 20, 50]"
          show-size-picker
          :page-slot="7"
          @update:page="load"
          @update:page-size="(v: number) => { pageSize = v; page = 1; load() }"
        />
      </div>
    </template>
  </NCard>

  <!-- 新增素材 -->
  <NModal v-model:show="formShow" preset="card" title="新增素材" style="max-width: 680px">
    <NAlert type="info" :bordered="false" style="margin-bottom: 14px">
      <div style="font-size: 12.5px; line-height: 1.7">
        存进去的内容会在写稿时<b>自动</b>被 AI 参考：命中相关的片段会注入模型上下文。
        适合放产品资料、FAQ、历史成稿、术语表、风格样例这类「反复要用」的内容。
      </div>
    </NAlert>
    <div class="kb-form">
      <div class="kb-row">
        <span class="kb-label">标题</span>
        <NInput v-model:value="formTitle" placeholder="留空自动取首行 / 文件名" maxlength="300" />
      </div>
      <div class="kb-row">
        <span class="kb-label">文档</span>
        <div class="kb-file">
          <NButton size="small" tertiary @click="fileInput?.click()">选择文件</NButton>
          <span v-if="formFile" class="kb-file-name">{{ formFile.name }}</span>
          <span v-else class="kb-file-hint">支持 PDF / TXT / Word / JSON / Markdown</span>
          <NButton
            v-if="formFile"
            size="tiny"
            text
            type="error"
            @click="formFile = null"
          >移除</NButton>
          <input
            ref="fileInput"
            type="file"
            accept=".pdf,.txt,.md,.markdown,.json,.doc,.docx"
            style="display: none"
            @change="onPickFile"
          />
        </div>
      </div>
      <div class="kb-row kb-row-wide">
        <span class="kb-label">内容</span>
        <NInput
          v-model:value="formText"
          type="textarea"
          :rows="12"
          :disabled="!!formFile"
          :placeholder="formFile ? '已选文档，将解析文档内容入库' : '粘贴或输入素材正文（支持 Markdown）'"
        />
      </div>
    </div>
    <template #footer>
      <NSpace justify="end">
        <NButton @click="formShow = false">取消</NButton>
        <IconTextBtn name="check" type="primary" :loading="submitting" @click="submitCreate">
          存入知识库
        </IconTextBtn>
      </NSpace>
    </template>
  </NModal>

  <!-- 详情：正文 + 切分结果 -->
  <NModal
    v-model:show="detailShow"
    preset="card"
    :title="detail ? `素材详情 · ${detail.title}` : '素材详情'"
    style="max-width: 760px"
  >
    <template v-if="detail">
      <div class="kb-detail-meta">
        <NText depth="3" style="font-size: 12px">
          {{ KNOWLEDGE_SOURCE_LABEL[detail.source] }} · {{ detail.char_count }} 字 ·
          {{ detail.chunk_count }} 片
        </NText>
        <NButton
          v-if="detail.source === 'content' && detail.origin_ref"
          size="small"
          tertiary
          @click="gotoSource"
        >
          查看来源成稿 →
        </NButton>
      </div>
      <div class="kb-section-title">切分结果</div>
      <div v-for="c in detail.chunks ?? []" :key="c.id" class="kb-chunk">
        <div class="kb-chunk-head">
          <span>#{{ c.seq + 1 }} · {{ c.char_len }} 字</span>
          <NTag
            size="small"
            :bordered="false"
            :type="c.status === 'ready' ? 'success' : c.status === 'failed' ? 'error' : 'default'"
          >{{ statusMeta[c.status]?.label ?? c.status }}</NTag>
        </div>
        <div class="kb-chunk-body">{{ c.content }}</div>
      </div>
      <div class="kb-section-title">原文</div>
      <pre class="kb-raw">{{ detail.raw_text }}</pre>
    </template>
  </NModal>

  <!-- 重命名 -->
  <NModal v-model:show="renameShow" preset="card" title="重命名" style="max-width: 480px">
    <NInput v-model:value="renameTitle" placeholder="素材标题" maxlength="300" />
    <template #footer>
      <NSpace justify="end">
        <NButton @click="renameShow = false">取消</NButton>
        <IconTextBtn name="check" type="primary" @click="submitRename">保存</IconTextBtn>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
.kb-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.kb-filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.kb-probe {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.kb-probe-result {
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.2));
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.kb-hit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.kb-hit-body {
  font-size: 12.5px;
  line-height: 1.7;
  opacity: 0.8;
  margin-top: 4px;
}
.kb-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
.kb-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kb-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.kb-row-wide {
  align-items: flex-start;
}
.kb-label {
  width: 48px;
  flex: none;
  font-size: 13px;
  opacity: 0.75;
  padding-top: 4px;
}
.kb-file {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.kb-file-name {
  font-size: 13px;
  color: var(--primary-color, #2080f0);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-file-hint {
  font-size: 12.5px;
  opacity: 0.55;
}
.kb-detail-meta {
  margin-bottom: 12px;
}
.kb-section-title {
  font-size: 13px;
  font-weight: 600;
  margin: 14px 0 8px;
}
.kb-chunk {
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.2));
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.kb-chunk-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  opacity: 0.75;
}
.kb-chunk-body {
  font-size: 12.5px;
  line-height: 1.7;
  margin-top: 6px;
  white-space: pre-wrap;
}
.kb-raw {
  white-space: pre-wrap;
  font-size: 12.5px;
  line-height: 1.7;
  max-height: 320px;
  overflow: auto;
  margin: 0;
  font-family: inherit;
}
</style>
