/**
 * 新手引导的步骤数据（全系统主线）。
 *
 * 每一步可以「先跳到某页」再「高亮某个元素」。设计约束：
 * - `anchor` 尽量用**视图里已有的语义 class**（如 `.chat-toolbar`），不为了做引导
 *   去改页面结构；锚点找不到时会自动退化成「屏幕居中的无高亮卡片」，不会崩。
 * - 文案只描述**当前真实存在**的功能，不写「即将上线 / 敬请期待」。
 *   近期新增的「知乎热榜灵感」「个人中心」「知乎热点配置」「服务健康实际模型」
 *   「收益看板」都要在这里同步，否则引导会和真实界面脱节。
 * - 移动端没有侧栏，导航高亮锚点另给 `anchorMobile`；其余锚点（如对话页、
 *   队列页的元素）桌面与窄屏共用同一个选择器即可。
 */
export interface TourStep {
  /** 先 `router.push({name: route})` 再定位；省略 = 不跳转 */
  route?: string
  /** 高亮目标选择器；省略 = 居中无高亮卡片 */
  anchor?: string
  /** 窄屏下改用这个选择器（如移动底栏替代桌面侧栏） */
  anchorMobile?: string
  title: string
  body: string
  /** 气泡相对高亮框的位置；默认 auto（自动挑空间最大的一侧） */
  placement?: 'auto' | 'top' | 'bottom' | 'left' | 'right'
}

export const TOUR_STEPS: TourStep[] = [
  {
    route: 'chat',
    title: '欢迎使用内容运营台',
    body: '这是一条「从想法到发布」的流水线：AI 帮你写 → 排版成可发布格式 → 推到公众号草稿箱。花 1 分钟走一遍，之后不会再自动出现。',
  },
  {
    route: 'chat',
    anchor: '.app-sider .n-menu',
    anchorMobile: '.mobile-tabbar',
    title: '六个模块，一条流水线',
    body:
      '对话创作 → 发布队列 → 视频生产 → AI 用量 → 收益看板 → 系统设置。\n' +
      '主线是前两个：先对话产出内容，再到队列里加工与发布。\n' +
      '顶栏头像点开还有「个人中心」（改资料、传头像）；「收益看板」看流量主广告进账；系统设置里也多了「知乎热点」配置。',
  },
  {
    route: 'chat',
    anchor: '.chat-toolbar',
    placement: 'bottom',
    title: '第一步：先选平台',
    body: '平台决定 AI 的写作规则与格式（公众号 / 小红书 / 抖音各不相同）。留空则是通用对话。',
  },
  {
    route: 'chat',
    anchor: '.welcome-hot',
    placement: 'bottom',
    title: '不知道写什么？看知乎热榜',
    body:
      '空白会话会自动列出知乎热榜 Top 5（每 30 分钟更新，本地读取零成本）。\n' +
      '点「以此选题」会把一句贴合平台的提问填进输入框，改改就能发；对话中途也能从工具栏「灵感」随时呼出。',
  },
  {
    route: 'chat',
    anchor: '.composer',
    placement: 'top',
    title: '第二步：提需求，回车发送',
    body:
      '用自然语言描述你想写什么即可，Shift+Enter 换行。\n' +
      'AI 回复下方有「保存到队列」——存进去才会进入排版与发布流程。',
  },
  {
    route: 'queue',
    anchor: '.queue-header',
    placement: 'bottom',
    title: '发布队列：集中管理成品',
    body:
      '按状态筛选（待加工 / 待发布 / 已发布），看总计与待发布数量，一键导出成 Markdown 清单。\n' +
      '手里已有写好的完整文案？点「新建内容」直接粘进入队，不用再走一遍对话。',
  },
  {
    route: 'queue',
    anchor: '.n-data-table',
    placement: 'top',
    title: '每行都能加工与发布',
    body:
      '「加工排版」把纯内容排成公众号正文（约 2-3 分钟，可配图、换主题），再「推草稿箱」。\n' +
      '已经发布的行会变成只读的「查看排版」，不会再被改动。',
  },
  {
    route: 'video',
    title: '视频生产：口播稿变视频',
    body: '把口播稿转成带配音与字幕的视频。渲染是异步的，提交后可以在页面里查看历史与进度。',
  },
  {
    route: 'usage',
    title: 'AI 用量：钱花在哪儿',
    body: '每次生成都会记录 token 与花费，这里按时间和模型汇总，方便你控制成本。',
  },
  {
    route: 'revenue',
    title: '收益看板：广告赚了多少',
    body:
      '流量主广告的收益、曝光、点击都在这里，并区分「已结算 / 待结算」。\n' +
      '数据 T+1 更新（当天未出），结算是累计口径、按月出账打款，所以「待结算」会一直有余额。\n' +
      '想从微信强制拉一次最新数据，点右上角「刷新」；想把数据带走，点「导出」可选 Excel 或 CSV。',
  },
  {
    route: 'settings',
    anchor: '.grid',
    placement: 'bottom',
    title: '系统设置：体检与配置',
    body:
      '「服务健康」会显示你实际在用的模型——DeepSeek 双模型（Flash 管文案、Pro 管推理）或你切到的 Cloudflare 通道。\n' +
      '管理员还能在「知乎热点」卡片看热榜额度、在线配密钥。想重看引导，点侧栏/头部 ❓。',
  },
]
