<script lang="ts">
import { h, defineComponent } from 'vue'
import { NButton, NTooltip } from 'naive-ui'
import IconSvg from '@/components/IconSvg.vue'

/**
 * 统一的图标操作按钮：圆形图标按钮 + hover tooltip。
 *
 * 全站所有「记录行操作」都走它，保证视觉一致（不再图标/文字混用）：
 *   - 模板里用：<ActionBtn name="trash" tip="删除" type="error" @click="..." />
 *   - h() 渲染里用：actionBtn('trash', '删除', fn, { type: 'error' })
 *
 * 透传 tag/href/download 是为了「下载成片」这种锚点按钮也能复用同一套样式。
 */
export default defineComponent({
  name: 'ActionBtn',
  props: {
    name: { type: String, required: true },
    tip: { type: String, required: true },
    type: {
      type: String as () => 'default' | 'error' | 'primary' | 'info' | 'warning' | 'success',
      default: 'default',
    },
    size: { type: String as () => 'tiny' | 'small', default: 'tiny' },
    loading: { type: Boolean, default: false },
    disabled: { type: Boolean, default: false },
    tag: { type: String, default: undefined },
    href: { type: String, default: undefined },
    download: { type: String, default: undefined },
  },
  emits: ['click'],
  setup(props, { emit }) {
    return () =>
      h(
        NTooltip,
        { trigger: 'hover' },
        {
          trigger: () =>
            h(
              NButton,
              {
                size: props.size,
                quaternary: true,
                circle: true,
                type: props.type,
                loading: props.loading,
                disabled: props.disabled,
                ...(props.tag
                  ? { tag: props.tag as 'a', href: props.href, download: props.download }
                  : {}),
                onClick: () => emit('click'),
              },
              { icon: () => h(IconSvg, { name: props.name, size: 16 }) },
            ),
          default: () => props.tip,
        },
      )
  },
})
</script>
