<script setup lang="ts">
/**
 * Markdown 渲染组件。
 *
 * 为什么需要它：AI 现在产出的是结构化 Markdown（标题/引用/列表/加粗），
 * 用 `{{ content }}` 直接渲染会把 `##` `**` 也显示出来，用户看到的是一堆符号。
 *
 * 安全：模型输出不可信，DOMPurify 清洗后再 v-html —— 防止模型被诱导输出
 * <script>/onerror 之类的注入。链接统一加 target=_blank + rel=noopener。
 */
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = defineProps<{ content: string }>()

const md = new MarkdownIt({
  html: false,      // 不允许原始 HTML 标签（模型不该输出 HTML，排版是后端的事）
  linkify: true,    // 自动识别 URL
  breaks: true,     // 单个换行也生效（符合聊天场景直觉）
  typographer: false,
})

// 链接新窗口打开，并切断 opener 引用
const defaultLinkOpen =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, idx, options, env, self)
}

const html = computed(() =>
  DOMPurify.sanitize(md.render(props.content || ''), {
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'del', 's', 'blockquote', 'code', 'pre',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li', 'hr', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    ],
    ALLOWED_ATTR: ['href', 'target', 'rel'],
  }),
)
</script>

<template>
  <div class="md-body" v-html="html" />
</template>

<style scoped>
/* 保证 markdown 产物在聊天区里排版自然，不引入全局样式 */
.md-body :deep(p) {
  margin: 0 0 10px;
  line-height: 1.75;
}
.md-body :deep(p:last-child) {
  margin-bottom: 0;
}
.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3) {
  margin: 14px 0 8px;
  font-weight: 600;
  line-height: 1.4;
}
.md-body :deep(h1) {
  font-size: 18px;
}
.md-body :deep(h2) {
  font-size: 16px;
}
.md-body :deep(h3) {
  font-size: 15px;
}
.md-body :deep(blockquote) {
  margin: 10px 0;
  padding: 6px 12px;
  border-left: 3px solid var(--app-border, #e0e0e6);
  background: rgba(128, 128, 128, 0.06);
  border-radius: 4px;
  color: inherit;
}
.md-body :deep(ul),
.md-body :deep(ol) {
  margin: 8px 0;
  padding-left: 22px;
}
.md-body :deep(li) {
  margin: 4px 0;
  line-height: 1.7;
}
.md-body :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.14);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.92em;
}
.md-body :deep(pre) {
  margin: 10px 0;
  padding: 10px 12px;
  border-radius: 6px;
  background: rgba(128, 128, 128, 0.12);
  overflow-x: auto;
}
.md-body :deep(pre code) {
  padding: 0;
  background: none;
}
.md-body :deep(hr) {
  margin: 14px 0;
  border: none;
  border-top: 1px solid var(--app-border, #e0e0e6);
}
.md-body :deep(a) {
  color: var(--app-primary, var(--app-primary));
  text-decoration: underline;
}
.md-body :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 0.94em;
}
.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid var(--app-border, #e0e0e6);
  padding: 4px 10px;
}
</style>
