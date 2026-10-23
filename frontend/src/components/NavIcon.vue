<script lang="ts">
/**
 * 导航图标。
 *
 * 统一用 stroke 线性图标（约 1.8 描边），而不是 emoji 或几何符号——
 * 后者在不同系统上渲染差异大、视觉重量也不统一。
 *
 * 用法：
 *   <NavIcon name="chat" />
 *   // 或在需要渲染函数的场合（如 NMenu 的 icon）
 *   icon: () => h(NavIcon, { name: 'chat' })
 */
import { defineComponent, h } from 'vue'

type Node = [string, Record<string, string | number>]

const NODES: Record<string, Node[]> = {
  // 对话气泡
  chat: [['path', { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' }]],
  // 队列列表（三条横线 + 左侧节点）
  queue: [
    ['line', { x1: '8', y1: '6', x2: '21', y2: '6' }],
    ['line', { x1: '8', y1: '12', x2: '21', y2: '12' }],
    ['line', { x1: '8', y1: '18', x2: '21', y2: '18' }],
    ['line', { x1: '3', y1: '6', x2: '3.01', y2: '6' }],
    ['line', { x1: '3', y1: '12', x2: '3.01', y2: '12' }],
    ['line', { x1: '3', y1: '18', x2: '3.01', y2: '18' }],
  ],
  // 视频：矩形 + 播放三角
  video: [
    ['rect', { x: '2', y: '5', width: '14', height: '14', rx: '2', ry: '2' }],
    ['polygon', { points: '23 7 16 12 23 17 23 7' }],
  ],
  // 活动脉冲：表达「健康/状态」比齿轮更贴切这个页面
  activity: [['polyline', { points: '22 12 18 12 15 21 9 3 6 12 2 12' }]],
  // 技能包：闪电（激活技能那一瞬），与「活动脉冲」的心电线区分开
  skills: [['polygon', { points: '13 2 3 14 12 14 11 22 21 10 12 10 13 2' }]],
  // 知识库：一本打开的书（两页 + 书脊）
  book: [
    ['path', { d: 'M2 4h6a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H2z' }],
    ['path', { d: 'M22 4h-6a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H22z' }],
  ],
  // 用量柱状：三根高低不同的柱子，一眼是「统计/消耗」
  usage: [
    ['line', { x1: '3', y1: '21', x2: '21', y2: '21' }],
    ['rect', { x: '5', y: '12', width: '4', height: '9', rx: '1' }],
    ['rect', { x: '11', y: '7', width: '4', height: '14', rx: '1' }],
    ['rect', { x: '17', y: '10', width: '2.5', height: '11', rx: '1' }],
  ],
  // 用户管理：一个人 + 身后半个人
  users: [
    ['path', { d: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2' }],
    ['circle', { cx: '9', cy: '7', r: '4' }],
    ['path', { d: 'M22 21v-2a4 4 0 0 0-3-3.87' }],
    ['path', { d: 'M16 3.13a4 4 0 0 1 0 7.75' }],
  ],
  // 收益看板：上升趋势线 + 人民币符号
  revenue: [
    ['polyline', { points: '3 17 9 11 13 15 21 7' }],
    ['polyline', { points: '15 7 21 7 21 13' }],
  ],
  // 模型池：三层堆叠（一眼「一池资源，可分层取用」），与「用量」的三根柱子区分
  layers: [
    ['polygon', { points: '12 2 22 8 12 14 2 8 12 2' }],
    ['polyline', { points: '2 12 12 18 22 12' }],
    ['polyline', { points: '2 16 12 22 22 16' }],
  ],
}

export default defineComponent({
  name: 'NavIcon',
  props: {
    name: { type: String, required: true },
    size: { type: [Number, String], default: 18 },
  },
  setup(props) {
    return () =>
      h(
        'svg',
        {
          class: 'nav-icon-svg',
          width: props.size,
          height: props.size,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke: 'currentColor',
          'stroke-width': '1.8',
          'stroke-linecap': 'round',
          'stroke-linejoin': 'round',
          'aria-hidden': 'true',
        },
        (NODES[props.name] ?? []).map(([tag, attrs]) => h(tag, attrs)),
      )
  },
})
</script>

<style scoped>
/* SVG 本身是 currentColor 描边，跟随父级文字色；不额外加样式，
   避免 scoped 属性选择器意外影响 SVG 内部图形元素 */
.nav-icon-svg {
  display: block;
}
</style>
