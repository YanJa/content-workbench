<script setup lang="ts">
/**
 * 轻量 Markdown 编辑器：NInput textarea + 一排工具栏按钮。
 *
 * 为什么自研不引库：项目依赖干净（只有 naive-ui / markdown-it / dompurify），
 * 需求只是「改完 Markdown 能存回去」+ 几个常用格式快捷按钮，
 * 引一个完整富文本编辑器反而重、还容易把 Markdown 结构改坏。
 *
 * 关键实现：所有按钮都是对 textarea 的选区做「包裹」或「行首加前缀」，
 * 改完 value 后必须 nextTick 恢复光标，否则用户光标会跳到末尾。
 */
import { nextTick, ref } from 'vue'
import { NButton, NInput, NTooltip, type InputInst } from 'naive-ui'

const props = defineProps<{
  modelValue: string
  disabled?: boolean
  /** 编辑区高度 */
  height?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const inputRef = ref<InputInst | null>(null)

/** 拿到真实 textarea DOM。 */
function getTextarea(): HTMLTextAreaElement | null {
  return inputRef.value?.textareaElRef ?? null
}

/** 回写并恢复光标（[selStart, selEnd]）。 */
async function commit(value: string, selStart: number, selEnd: number) {
  emit('update:modelValue', value)
  await nextTick()
  const ta = getTextarea()
  if (ta) {
    ta.focus()
    ta.setSelectionRange(selStart, selEnd)
  }
}

/** 用 prefix/suffix 包裹选中文本（没选中就插入占位文字并选中它）。 */
async function wrap(prefix: string, suffix: string, placeholder = '文本') {
  const ta = getTextarea()
  if (!ta || props.disabled) return
  const value = props.modelValue || ''
  const start = ta.selectionStart
  const end = ta.selectionEnd
  const selected = value.slice(start, end) || placeholder
  const next = value.slice(0, start) + prefix + selected + suffix + value.slice(end)
  const s = start + prefix.length
  await commit(next, s, s + selected.length)
}

/** 给选中涉及的每一行行首加前缀（标题/引用/列表）。 */
async function prefixLines(prefix: string) {
  const ta = getTextarea()
  if (!ta || props.disabled) return
  const value = props.modelValue || ''
  const start = ta.selectionStart
  const end = ta.selectionEnd
  // 扩展到整行边界
  const lineStart = value.lastIndexOf('\n', start - 1) + 1
  const lineEnd = value.indexOf('\n', end) === -1 ? value.length : value.indexOf('\n', end)
  const block = value.slice(lineStart, lineEnd)
  const prefixed = block
    .split('\n')
    .map((l) => (l.startsWith(prefix) ? l : prefix + l))
    .join('\n')
  const next = value.slice(0, lineStart) + prefixed + value.slice(lineEnd)
  await commit(next, lineStart, lineStart + prefixed.length)
}

/** 插入链接：选中文字变 [文字](url)，光标停在 url 处。 */
async function insertLink() {
  const ta = getTextarea()
  if (!ta || props.disabled) return
  const value = props.modelValue || ''
  const start = ta.selectionStart
  const end = ta.selectionEnd
  const text = value.slice(start, end) || '链接文字'
  const snippet = `[${text}](url)`
  const next = value.slice(0, start) + snippet + value.slice(end)
  // 光标落在 url 上，方便直接替换
  const urlStart = start + 1 + text.length + 2
  await commit(next, urlStart, urlStart + 3)
}

/** 插入水平分割线（独占一行）。 */
async function insertHr() {
  const ta = getTextarea()
  if (!ta || props.disabled) return
  const value = props.modelValue || ''
  const start = ta.selectionStart
  const needNl = start > 0 && value[start - 1] !== '\n'
  const snippet = (needNl ? '\n' : '') + '---\n'
  const next = value.slice(0, start) + snippet + value.slice(start)
  const pos = start + snippet.length
  await commit(next, pos, pos)
}
</script>

<template>
  <div class="md-editor">
    <div class="md-toolbar">
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="wrap('**', '**', '加粗')">
            <b>B</b>
          </NButton>
        </template>
        加粗
      </NTooltip>
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="wrap('*', '*', '斜体')">
            <i>I</i>
          </NButton>
        </template>
        斜体
      </NTooltip>
      <span class="sep" />
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="prefixLines('## ')">
            H2
          </NButton>
        </template>
        二级标题
      </NTooltip>
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="prefixLines('### ')">
            H3
          </NButton>
        </template>
        三级标题
      </NTooltip>
      <span class="sep" />
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="prefixLines('> ')">
            引用
          </NButton>
        </template>
        引用块
      </NTooltip>
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="prefixLines('- ')">
            列表
          </NButton>
        </template>
        无序列表
      </NTooltip>
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="insertLink">
            链接
          </NButton>
        </template>
        插入链接
      </NTooltip>
      <NTooltip trigger="hover" :delay="400">
        <template #trigger>
          <NButton size="tiny" quaternary :disabled="disabled" @click="insertHr">
            分割线
          </NButton>
        </template>
        水平分割线
      </NTooltip>
      <span class="md-hint">Markdown 源码，排版时按其结构生成公众号样式</span>
    </div>
    <NInput
      ref="inputRef"
      type="textarea"
      :value="modelValue"
      :disabled="disabled"
      :style="{ height: height || '440px' }"
      placeholder="在这里编辑 Markdown 正文…"
      @update:value="(v: string) => emit('update:modelValue', v)"
    />
  </div>
</template>

<style scoped>
.md-editor {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.md-toolbar {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 2px 0 6px;
  flex-wrap: wrap;
}
.md-toolbar .sep {
  width: 1px;
  height: 14px;
  background: var(--app-border, #e0e0e6);
  margin: 0 4px;
}
.md-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--app-text-3);
}
.md-editor :deep(.n-input) {
  flex: 1;
  min-height: 0;
}
.md-editor :deep(.n-input textarea) {
  height: 100% !important;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.7;
}
</style>
