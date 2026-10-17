<script lang="ts">
import { defineComponent } from 'vue'
import { icon } from '@/utils/icon'

/**
 * 把 utils/icon.ts 里的内联 SVG 包成一个可用在模板里的组件：
 * <IconSvg name="trash" /> —— 既能在 <ActionBtn> 内部用，也能裸用在
 * NPopconfirm 的 trigger 按钮里（避免 tooltip 套 popconfirm 的嵌套坑）。
 */
export default defineComponent({
  name: 'IconSvg',
  props: {
    name: { type: String, required: true },
    size: { type: Number, default: 16 },
  },
  setup(props) {
    return () => icon(props.name, props.size)
  },
})
</script>
