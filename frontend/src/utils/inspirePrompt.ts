/**
 * 「选中灵感」后填进输入框的那句话。
 *
 * 为什么单独抽出来：这段是纯字符串逻辑，不依赖 Vue / 网络，能单独跑样例核对
 * 语气，也方便以后按真实反馈调句式而不碰 ChatView 的交互。
 *
 * 三个设计原则：
 *
 * 1. **带上灵感自己的上下文**。旧版只把标题塞进去，模型看不到这条热榜到底在
 *    讲什么，只能凭标题编；编出来的东西必然空、必然套话。这里用平台白送的
 *    summary（清洗 + 截断）+ 趋势信号，让「符合内容场景」有素材可依。
 * 2. **像人在说话，不像在给 AI 下工单**。旧版「围绕知乎热榜话题「X」写一篇…，
 *    先给我 3 个角度」是典型的模板变量 + 指令清单。更麻烦的是「给 3 个角度」
 *    和后端 BASE_SYSTEM 的「严禁让用户做选择题 / 正文写完立即结束」正面冲突，
 *    模型会纠结，产出就更容易端着。这里改成第一人称的一句话，只给方向不给清单。
 * 3. **去 AI 味不重复造轮子**。后端 BASE_SYSTEM 已有完整的写作姿态要求
 *    （第一人称、不列 1234、删机械连接词、去宏大词、留个人气味），这里不再
 *    复述一遍，只补它管不到的两件事：这条话题的具体背景，以及题材分寸。
 *
 * 仍然保持 HotInspiration 组件定下的交互：只填输入框、不自动发送、用户可改。
 */

export type InspireSource = 'zhihu' | 'weibo'

/** 灵感卡能提供的上下文（字段与 api/zhihu.ts 的 InspireItem 对齐） */
export interface InspireContext {
  title: string
  /** 平台摘要：知乎有，微博无；可能带链接、[图片]、@媒体等噪声 */
  summary?: string | null
  is_new?: boolean
  climb?: number
  staying_hours?: number
  hot?: number
}

/** 当前创作场景：平台 + 各平台子形态（与 ChatView 的 ref 一致） */
export interface InspireScene {
  platform: string
  gzhSub?: string
  zhihuSub?: string
  toutiaoSub?: string
  baijiahaoSub?: string
}

/**
 * 严肃题材信号。命中时补一句分寸提醒：热榜里常混着事故、诉讼、维权、
 * 人物争议，用小红书式的轻快语气去写会非常不合时宜。
 *
 * 只用中性的题材类别词，不做价值判断，也不硬给话题分类 —— 真正的语气判断
 * 交给模型，这里只是提醒它「先掂量一下」。
 */
const SERIOUS_RE =
  /事故|伤亡|遇难|身亡|逝世|去世|死亡|起诉|诉讼|判决|判刑|拘留|处罚|通报|调查|争议|道歉|维权|抵制|泄露|诈骗|违规|纠纷|灾害|地震|火灾|中毒|召回|停职|辞职|举报|被捕|逮捕|涉嫌/

/** 摘要清洗：去掉热榜摘要常见噪声，截到能一眼读完的长度。 */
export function cleanSummary(raw?: string | null, max = 72): string {
  if (!raw) return ''
  const s = raw
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\[图片\]|\[视频\]|\[链接\]|\[贴图\]/g, '')
    .replace(/@[\w\u4e00-\u9fa5-]{1,20}/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!s) return ''
  if (s.length <= max) return s
  const cut = s.slice(0, max)
  // 尽量在句读处收尾，别把一个词劈成半截
  const last = Math.max(cut.lastIndexOf('。'), cut.lastIndexOf('，'), cut.lastIndexOf('；'))
  let out = last >= max * 0.6 ? cut.slice(0, last + 1) : `${cut.trimEnd()}…`
  // 断在逗号/分号上读起来像话没说完，换成省略号
  if (/[，；、,]$/.test(out)) out = `${out.slice(0, -1)}…`
  return out
}

/**
 * 热度语境：一句自然的前提，顺带把写法倾向藏在里面。
 * 刚上榜的趁热别写长；挂了一整天的，别人说过的别再重复。
 */
export function heatLine(t: InspireContext, source: InspireSource): string {
  const where = source === 'weibo' ? '微博热搜' : '知乎热榜'
  if (t.is_new) return `刚冒上${where}，还热着`
  if ((t.climb ?? 0) >= 3) return `在${where}上正往上冲`
  if ((t.staying_hours ?? 0) >= 12) return `在${where}挂了一天多，别人该说的差不多说过了`
  if (source === 'weibo' && (t.hot ?? 0) >= 1e7) return `在${where}上挺炸`
  return `今天还在${where}上`
}

/**
 * 各平台 / 子形态的主体句。第一人称、只给方向，不列「先给几个、再给几个」。
 * 形态约束（字数、纯文本、口播时长）融进口语里，而不是写成需求清单。
 */
function bodyFor(scene: InspireScene): string {
  const p = scene.platform
  if (p === 'gzh') {
    if (scene.gzhSub === 'tietu')
      return '我想拿它做条贴图：5-6 张图配一段短话，第一屏那句得让人愿意停一下。说一个我自己的观察就够了，别往大道理上拔。'
    return '我想写篇公众号。别复述一遍新闻，挑一个我能说透的角度往里聊，落到具体的人和事上。'
  }
  if (p === 'xiaohongshu')
    return '我想发篇小红书，就当跟朋友聊起这事：它跟普通人有什么关系、换成我会怎么做。别写成种草模板。'
  if (p === 'douyin')
    return '我想做成一条 60 秒口播，开头三两句得把人留住。用平时说话的劲儿，别念稿。'
  if (p === 'zhihu') {
    if (scene.zhihuSub === 'article')
      return '我想写篇知乎专栏，把这事掰开讲清楚：来龙去脉、我怎么看、别人照着能怎么用。别攒成资料汇编。'
    if (scene.zhihuSub === 'idea')
      return '我想发条知乎想法，不铺开，只说我刚注意到的一个细节，百来字收住。'
    return '我想写个知乎回答，就当真有人问到我：从自己经历过的事聊起，当时怎么想、后来怎么变，别给标准答案。'
  }
  if (p === 'toutiao') {
    if (scene.toutiaoSub === 'weitoutiao')
      return '我想发条微头条，不写标题，头两行就把话说到位，像跟陌生人搭句话。'
    return '我想写篇头条图文，开头别铺垫，先给个让人愿意往下读的由头，后面全是实在的。'
  }
  if (p === 'baijiahao') {
    if (scene.baijiahaoSub === 'dongtai')
      return '我想发条百家号动态，一个看法配张图，随手说，别写成小作文。'
    return '我想写篇百家号长文，把关键词放前面让人搜得到，内容别绕，读完能记住点什么。'
  }
  // 没选平台：不给选择题，让模型自己定平台直接出成稿（对齐 BASE_SYSTEM 交付纪律）
  return '我想拿这事写点东西，你掂量下它更适合发哪儿，直接按那个路子给我一版成稿。'
}

/** 拼出最终填进输入框的文本。背景独立成段，用户一眼能看见、也能整段删掉。 */
export function buildInspirePrompt(
  t: InspireContext,
  source: InspireSource,
  scene: InspireScene,
): string {
  // 标题里自带的「」换成『』，避免和外层引号套在一起
  const title = (t.title || '').trim().replace(/「/g, '『').replace(/」/g, '』')
  const bg = cleanSummary(t.summary)
  const serious = SERIOUS_RE.test(`${title} ${bg}`)

  // 第一段：这是什么话题 + 我掌握的全部事实。放前面，模型先有素材再听任务，
  // 不再出现「只看到标题、只能凭空编」；用户想删背景也是整段的事。
  const context = `「${title}」${heatLine(t, source)}。` + (bg ? `我手上就这点背景：${bg}` : '')
  // 第二段：这一版要写成什么。放末尾，任务指令权重最高。
  const task =
    bodyFor(scene) +
    (serious ? '这事牵扯到具体的人和是非，分寸收着点，别玩梗也别替谁下结论。' : '')

  return `${context}\n\n${task}`
}

/**
 * 「待答问题」入口。它没有摘要字段，但问题本身就是最具体的场景，
 * 所以靠问题的原始措辞把模型摁在「答这一题」上，而不是泛泛而谈。
 */
export function buildQuestionPrompt(title: string): string {
  return `「${(title || '').trim()}」这题我想认真答一下。就当真有人问到我，从自己经历过的事聊起：当时怎么选的、踩过什么坑、现在回头看值不值。像写给一个正纠结的朋友，别写成标准答案。`
}
