import { h, type VNode } from 'vue'
import ActionBtn from '@/components/ActionBtn.vue'

export type ActionBtnType = 'default' | 'error' | 'primary' | 'info' | 'warning' | 'success'
export type ActionBtnSize = 'tiny' | 'small'

interface ActionBtnOpts {
  type?: ActionBtnType
  size?: ActionBtnSize
  loading?: boolean
  disabled?: boolean
  tag?: string
  href?: string
  download?: string
}

/**
 * 在 h() 渲染函数里生成统一的图标操作按钮（与 <ActionBtn> 组件同一实现）。
 * 用法：
 *   actionBtn('trash', '删除', () => onDelete(row), { type: 'error' })
 */
export function actionBtn(
  name: string,
  tip: string,
  onClick: () => void,
  opts: ActionBtnOpts = {},
): VNode {
  return h(ActionBtn, {
    name,
    tip,
    type: opts.type ?? 'default',
    size: opts.size ?? 'tiny',
    loading: opts.loading ?? false,
    disabled: opts.disabled ?? false,
    tag: opts.tag,
    href: opts.href,
    download: opts.download,
    onClick,
  })
}
