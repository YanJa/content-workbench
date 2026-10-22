<script setup lang="ts">
/**
 * 技能包（Skill）页。
 *
 * 这一页要回答用户三个问题，顺序很重要：
 *   1. 我装了哪些技能？（列表 + 启用状态）
 *   2. 它到底什么时候生效？（每个技能的 description 摊开写清楚 —— 那是模型
 *      判断「要不要用」的唯一依据，藏起来用户就会瞎装）
 *   3. 为什么这个没生效？（invalid 的直接把 error 摆在行里，不藏在弹窗里）
 *
 * 刻意不做一个「技能市场」：阶段一只支持用户自己的技能，装别人的抄一份
 * SKILL.md 进来即可。做一个假市场比没有更糟。
 */
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert,
  NCard,
  NCode,
  NDataTable,
  NEmpty,
  NInput,
  NModal,
  NSwitch,
  NTag,
  NText,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'

import ActionBtn from '@/components/ActionBtn.vue'
import IconTextBtn from '@/components/IconTextBtn.vue'
import { usePermGate } from '@/composables/usePermGate'
import {
  SKILL_EXAMPLE,
  createSkill,
  deleteSkill,
  listSkills,
  patchSkill,
  skillsMeta,
  uploadSkill,
  type Skill,
  type SkillsMeta,
} from '@/api/skills'

const message = useMessage()
const dialog = useDialog()

/** 增删改启停都归同一个权限点，与后端 require_use("skill.manage") 口径一致 */
const manageGate = usePermGate({ perm: 'skill.manage' })

const skills = ref<Skill[]>([])
const meta = ref<SkillsMeta | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const [list, m] = await Promise.all([listSkills(), skillsMeta()])
    skills.value = list
    meta.value = m
  } catch (e) {
    message.error((e as Error).message || '加载技能失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

const activeCount = computed(
  () => skills.value.filter((s) => s.status === 'ready' && s.enabled).length,
)

/** catalog 里实际会列出去的条数（受后端 SKILL_CATALOG_MAX 限制） */
const inCatalog = computed(() =>
  Math.min(activeCount.value, meta.value?.catalog_max ?? activeCount.value),
)

// ---------------------------------------------------------------- 新增 / 编辑
const formShow = ref(false)
const formText = ref('')
const formSaving = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

function openCreate() {
  formText.value = ''
  formShow.value = true
}

function fillExample() {
  formText.value = SKILL_EXAMPLE
}

async function onPickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许连续选同一个文件
  if (!file) return
  formSaving.value = true
  try {
    const s = await uploadSkill(file)
    formShow.value = false
    if (s.status === 'invalid') {
      // 解析失败不是「传失败了」—— 文件已经存下，只是配置有问题。
      // 用 warning 而不是 error，并把原因说清楚。
      message.warning(`已上传，但配置有误：${s.error}`)
    } else {
      message.success(`已导入技能「${s.name}」`)
    }
    await load()
  } catch (err) {
    message.error((err as Error).message || '上传失败')
  } finally {
    formSaving.value = false
  }
}

async function submitText() {
  if (!formText.value.trim()) {
    message.warning('先写点内容，或点「用示例填一下」')
    return
  }
  formSaving.value = true
  try {
    const s = await createSkill(formText.value)
    formShow.value = false
    if (s.status === 'invalid') {
      message.warning(`已保存，但配置有误：${s.error}`)
    } else {
      message.success(`已保存技能「${s.name}」`)
    }
    await load()
  } catch (e) {
    message.error((e as Error).message || '保存失败')
  } finally {
    formSaving.value = false
  }
}

// ---------------------------------------------------------------- 详情
const detailShow = ref(false)
const detail = ref<Skill | null>(null)

function openDetail(s: Skill) {
  detail.value = s
  detailShow.value = true
}

// ---------------------------------------------------------------- 行内操作
async function toggle(s: Skill, enabled: boolean) {
  try {
    await patchSkill(s.id, { enabled })
    s.enabled = enabled
    message.success(enabled ? `已启用「${s.name}」` : `已停用「${s.name}」`)
  } catch (e) {
    message.error((e as Error).message || '操作失败')
  }
}

const renaming = ref<Skill | null>(null)
const renameValue = ref('')

function startRename(s: Skill) {
  renaming.value = s
  renameValue.value = s.name
}

async function doRename() {
  const s = renaming.value
  if (!s) return
  const name = renameValue.value.trim().toLowerCase()
  if (!name || name === s.name) {
    renaming.value = null
    return
  }
  try {
    const updated = await patchSkill(s.id, { name })
    Object.assign(s, updated)
    renaming.value = null
    message.success('已改名')
  } catch (e) {
    message.error((e as Error).message || '改名失败')
  }
}

function confirmDelete(s: Skill) {
  dialog.warning({
    title: '删除技能',
    content: `确定删除「${s.name}」？删除后模型就不再用它了，可以重新上传恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deleteSkill(s.id)
        message.success('已删除')
        await load()
      } catch (e) {
        message.error((e as Error).message || '删除失败')
      }
    },
  })
}

// ---------------------------------------------------------------- 表格
const columns = computed<DataTableColumns<Skill>>(() => [
  {
    title: '技能',
    key: 'name',
    width: 210,
    render: (row) =>
      h('div', { style: 'display:flex;flex-direction:column;gap:2px' }, [
        h(
          'span',
          {
            style:
              'font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px',
          },
          row.name,
        ),
        h(
          NText,
          { depth: 3, style: 'font-size:11.5px' },
          { default: () => `用了 ${row.use_count} 次` },
        ),
      ]),
  },
  {
    title: '什么时候用（description）',
    key: 'description',
    minWidth: 320,
    render: (row) =>
      row.status === 'invalid'
        ? h(
            NText,
            { type: 'error', style: 'font-size:12.5px;line-height:1.6' },
            { default: () => row.error },
          )
        : h(
            'div',
            { style: 'font-size:12.5px;line-height:1.6;opacity:.85' },
            row.description,
          ),
  },
  {
    title: '状态',
    key: 'status',
    width: 100,
    render: (row) => {
      if (row.status === 'invalid') {
        return h(NTag, { size: 'small', type: 'error', bordered: false }, { default: () => '配置有误' })
      }
      return h(
        NTag,
        {
          size: 'small',
          type: row.enabled ? 'success' : 'default',
          bordered: false,
        },
        { default: () => (row.enabled ? '已启用' : '已停用') },
      )
    },
  },
  {
    title: '启用',
    key: 'enabled',
    width: 74,
    render: (row) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        disabled: row.status === 'invalid' || manageGate.blocked.value,
        'onUpdate:value': (v: boolean) => void toggle(row, v),
      }),
  },
  {
    title: '操作',
    key: 'ops',
    width: 130,
    render: (row) =>
      h('div', { style: 'display:flex;gap:6px' }, [
        h(ActionBtn, {
          name: 'eye',
          tip: '查看说明',
          onClick: () => openDetail(row),
        }),
        h(ActionBtn, {
          name: 'edit',
          tip: '改名',
          disabled: manageGate.blocked.value,
          onClick: () => startRename(row),
        }),
        h(ActionBtn, {
          name: 'trash',
          tip: '删除',
          type: 'error',
          disabled: manageGate.blocked.value,
          onClick: () => confirmDelete(row),
        }),
      ]),
  },
])
</script>

<template>
  <NCard>
    <template #header>
      <div class="sk-header">
        <div class="sk-head-left">
          <NTag size="small" :bordered="false">{{ skills.length }} 个技能</NTag>
          <NTag size="small" type="success" :bordered="false">
            生效中 {{ inCatalog }}
          </NTag>
        </div>
        <div style="display: flex; gap: 8px">
          <IconTextBtn
            name="plus"
            size="small"
            type="primary"
            :disabled="manageGate.blocked.value"
            @click="openCreate"
          >新增技能</IconTextBtn>
          <IconTextBtn name="reset" size="small" @click="load">刷新</IconTextBtn>
        </div>
      </div>
    </template>

    <NAlert v-if="meta && !meta.enabled" type="warning" :bordered="false" style="margin-bottom: 14px">
      技能功能已被服务端关闭（<code>ENABLE_SKILLS=false</code>），下面的技能不会生效。
    </NAlert>

    <NAlert type="info" :bordered="false" style="margin-bottom: 14px">
      <div style="font-size: 12.5px; line-height: 1.75">
        技能 = 一份 <code>SKILL.md</code>：开头两行元信息，后面是具体做法。
        <b>平时只把名字和「什么时候用」告诉 AI</b>（几乎不占上下文），
        等它觉得当前任务匹配，才会去读完整说明照着做。
        所以 <code>description</code> 要写清「做什么 + 什么场景用」——
        它是 AI 决定要不要用这个技能的唯一依据。
      </div>
    </NAlert>

    <NEmpty
      v-if="!skills.length && !loading"
      description="还没有技能。装一个之后，AI 遇到匹配的任务会自动读它的说明照着做。"
    />
    <NDataTable
      v-else
      :columns="columns"
      :data="skills"
      :loading="loading"
      :row-key="(r: Skill) => r.id"
      :scroll-x="960"
      size="small"
    />
  </NCard>

  <!-- 新增技能 -->
  <NModal
    v-model:show="formShow"
    preset="card"
    title="新增技能"
    style="max-width: 760px"
  >
    <div class="sk-form">
      <div class="sk-upload">
        <div class="sk-upload-hint">
          从别处拿到的技能包，直接把 <code>SKILL.md</code> 传上来即可；
          文件名的前缀必须和 frontmatter 里的 <code>name</code> 一致
          （官方规范：name 必须等于父目录名）。
        </div>
        <IconTextBtn
          name="upload"
          size="small"
          :loading="formSaving"
          @click="fileInput?.click()"
        >选择 SKILL.md</IconTextBtn>
        <input
          ref="fileInput"
          type="file"
          accept=".md,.markdown,.txt"
          style="display: none"
          @change="onPickFile"
        />
      </div>

      <div class="sk-or"><span>或者直接写</span></div>

      <NInput
        v-model:value="formText"
        type="textarea"
        :autosize="{ minRows: 12, maxRows: 22 }"
        placeholder="---&#10;name: my-skill&#10;description: 做什么，什么时候用&#10;---&#10;&#10;# 具体做法&#10;..."
        style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px"
      />
      <div class="sk-form-foot">
        <NText depth="3" style="font-size: 12px">
          同名技能会被最新内容覆盖；若新内容格式有误，原技能保持不变
        </NText>
        <div style="display: flex; gap: 8px">
          <IconTextBtn name="magic" size="small" @click="fillExample">用示例填一下</IconTextBtn>
          <IconTextBtn
            name="check"
            size="small"
            type="primary"
            :loading="formSaving"
            @click="submitText"
          >保存</IconTextBtn>
        </div>
      </div>
    </div>
  </NModal>

  <!-- 改名 -->
  <NModal
    :show="!!renaming"
    preset="dialog"
    title="技能改名"
    positive-text="保存"
    negative-text="取消"
    @positive-click="doRename"
    @negative-click="renaming = null"
    @close="renaming = null"
  >
    <div class="sk-rename">
      <NInput
        v-model:value="renameValue"
        placeholder="只能用小写字母、数字和连字符"
        @keyup.enter="doRename"
      />
      <NText depth="3" style="font-size: 12px">
        改名只是换个标识，正文不变。正文里的 name 不会跟着改 ——
        但它只在导入时校验，之后以这里为准。
      </NText>
    </div>
  </NModal>

  <!-- 详情 -->
  <NModal
    v-model:show="detailShow"
    preset="card"
    :title="detail ? `技能：${detail.name}` : '技能'"
    style="max-width: 780px"
  >
    <template v-if="detail">
      <div class="sk-detail-meta">
        <div class="sk-detail-row">
          <span class="sk-detail-label">什么时候用</span>
          <span class="sk-detail-value">{{ detail.description || '—' }}</span>
        </div>
        <div class="sk-detail-row">
          <span class="sk-detail-label">状态</span>
          <span class="sk-detail-value">
            <NTag
              v-if="detail.status === 'invalid'"
              size="small"
              type="error"
              :bordered="false"
            >配置有误</NTag>
            <NTag
              v-else
              size="small"
              :type="detail.enabled ? 'success' : 'default'"
              :bordered="false"
            >{{ detail.enabled ? '已启用' : '已停用' }}</NTag>
            <span style="margin-left: 10px; font-size: 12px; opacity: 0.65">
              被激活 {{ detail.use_count }} 次
            </span>
          </span>
        </div>
        <div v-if="detail.status === 'invalid'" class="sk-detail-row">
          <span class="sk-detail-label">问题</span>
          <span class="sk-detail-value" style="color: var(--n-text-color-error, #d03050)">
            {{ detail.error }}
          </span>
        </div>
      </div>
      <div class="sk-detail-body-title">完整说明（AI 激活时读到的原文）</div>
      <NCode
        :code="detail.body || '（无正文）'"
        language="markdown"
        word-wrap
        style="max-height: 44vh; overflow: auto"
      />
    </template>
  </NModal>
</template>

<style scoped>
.sk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.sk-head-left {
  display: flex;
  gap: 8px;
  align-items: center;
}
.sk-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sk-upload {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border: 1px dashed var(--border-color, rgba(128, 128, 128, 0.28));
  border-radius: 8px;
  flex-wrap: wrap;
}
.sk-upload-hint {
  flex: 1;
  min-width: 240px;
  font-size: 12.5px;
  line-height: 1.7;
  opacity: 0.8;
}
.sk-or {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  opacity: 0.5;
}
.sk-or::before,
.sk-or::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-color, rgba(128, 128, 128, 0.2));
}
.sk-form-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.sk-detail-meta {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
}
.sk-detail-row {
  display: flex;
  gap: 12px;
  font-size: 13px;
  line-height: 1.7;
}
.sk-detail-label {
  width: 68px;
  flex: none;
  opacity: 0.6;
}
.sk-detail-value {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}
.sk-detail-body-title {
  font-size: 12px;
  opacity: 0.6;
  margin-bottom: 6px;
}
.sk-rename {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
