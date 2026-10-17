<script setup lang="ts">
import { NButton } from 'naive-ui'
import type { PropType } from 'vue'
import IconSvg from './IconSvg.vue'

type BtnType = 'default' | 'primary' | 'error' | 'info' | 'success' | 'warning'
type BtnSize = 'tiny' | 'small' | 'medium' | 'large'

/**
 * 统一的「文字 + 左侧图标」按钮：用于页面级主操作（新建 / 添加 / 保存 / 生成 等）。
 * 与行内操作（ActionBtn 圆形图标 + tooltip）形成清晰分工——页面 CTA 用文字图标，
 * 表格行内动作用纯图标，全站视觉语言一致。
 */
defineProps({
  name: { type: String, required: true },
  type: { type: String as PropType<BtnType>, default: 'default' },
  size: { type: String as PropType<BtnSize>, default: 'small' },
  ghost: { type: Boolean, default: false },
  dashed: { type: Boolean, default: false },
  quaternary: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  block: { type: Boolean, default: false },
  iconSize: { type: Number, default: 16 },
  // 链接型按钮（如文件下载）
  tag: { type: String, default: undefined },
  href: { type: String, default: undefined },
  download: { type: String, default: undefined },
})

const emit = defineEmits<{ (e: 'click'): void }>()
</script>

<template>
  <NButton
    :type="type"
    :size="size"
    :ghost="ghost"
    :dashed="dashed"
    :quaternary="quaternary"
    :disabled="disabled"
    :loading="loading"
    :block="block"
    :tag="(tag as any)"
    :href="href"
    :download="download"
    @click="emit('click')"
  >
    <template #icon>
      <IconSvg :name="name" :size="iconSize" />
    </template>
    <slot />
  </NButton>
</template>
