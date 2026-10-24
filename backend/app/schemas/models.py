"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 会话
class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=200)
    scene: Literal["chat", "gzh", "xiaohongshu", "douyin", "zhihu"] = "chat"


class ConversationOut(BaseModel):
    id: str
    title: str
    scene: str
    summary: str = ""
    created_at: int
    updated_at: int

    model_config = {"from_attributes": True}


class ConversationRename(BaseModel):
    """重命名会话。留空或纯空白由服务层回落到「新对话」。"""

    title: str = Field(default="", max_length=200)


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    tool_name: str | None = None
    created_at: int
    # token 用量：前端打开历史会话时能还原当条消息的用量展示
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 推理模型的思维链（历史回显「思考过程」折叠块用）
    reasoning: str = ""
    # 模型调用失败的错误信息（该轮回复挂的红色提示；历史回显仍可见）
    error: str = ""
    # 展示型工具痕迹：{"calls": [{id, name, arguments, result, status}]}
    # 前端直接映射成 toolCalls，无需解析 OpenAI 格式
    tool_calls: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


# ---------------------------------------------------------------- 对话
class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    platform: Literal["gzh", "xiaohongshu", "douyin", "zhihu"] | None = None
    # 平台内子形态（目前只有知乎用：answer / article / idea）。
    # 传了非知乎平台会被后端归一成空串，不报错 —— 前端多带一个字段不该炸。
    sub_type: str = ""
    # 知乎回答归属的问题（URL 或纯 ID）。非空时后端拉一次同问题头部回答，
    # 作为「已有答案边界」注入 —— 这是「必须写出增量」能落地的前提。
    # 仅在 platform=zhihu + sub_type=answer 时生效。
    zhihu_question: str = ""
    topic: str = ""
    audience: str = ""
    # 是否允许模型调用工具
    enable_tools: bool = True
    # 前端为这条回复预生成的消息 id，落库时直接当主键。
    # 于是「保存到队列」上报的 msg_id 与历史消息 id 一致，队列页才能反跳定位。
    assistant_msg_id: str | None = None
    # 本轮是否从私有知识库召回素材（默认开，前端输入框旁开关控制）。
    # null 视为 true：老客户端不带这个字段时保持「自动召回」的默认体验。
    use_knowledge: bool | None = None
    # 本轮是否挂载技能包（默认开）。关掉后既不注入 catalog，也不提供
    # activate_skill 工具 —— 相当于临时把技能全停用一轮。
    use_skills: bool | None = None


# ---------------------------------------------------------------- 知识库
class KnowledgeCreate(BaseModel):
    """新增一条素材。

    两种互斥填法：
      - `text`：直接给原文（手动录入 / 抓取网页内容）；
      - `content_id`：复用工作台已产成稿（后端自己取 Content.body）。
    两个都给时以 text 为准——前端编辑框里的内容比库里的更新。
    """

    title: str = Field(default="", max_length=300)
    text: str = ""
    content_id: str | None = None
    source: Literal["manual", "content", "upload", "url"] = "manual"
    origin_ref: str = ""


class KnowledgeUpdate(BaseModel):
    """目前只开放改标题：改内容等于重切重算向量，用户该删了重加。"""

    title: str = Field(default="", max_length=300)


class KnowledgeSearchRequest(BaseModel):
    """召回调试用：看看某句话能命中哪些素材。"""

    query: str
    top_k: int = Field(default=5, ge=1, le=20)


class ToolConfirmRequest(BaseModel):
    # 可选：对话页确认时传真实会话 id（结果回写消息流）；队列页 / 加工面板的
    # 「推送草稿」不来自任何对话，不传即可。
    conversation_id: str | None = None
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


# ---------------------------------------------------------------- 内容
class ContentOut(BaseModel):
    id: str
    platform: str
    kind: str
    # 子形态（知乎：answer / article / idea；公众号：article / tietu；其余平台空串）
    sub_type: str = ""
    title: str
    body: str
    summary: str = ""
    meta: dict[str, Any] | None = None
    conversation_id: str | None = None
    # ---- 排版产物（队列加工后才有值） ----
    body_html: str = ""
    theme: str | None = None
    layouts: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    layout_status: str = "none"
    # 加工中标记："" 空闲 / "layout" 排版中 / "image" 生图中
    processing: str = ""
    created_at: int
    updated_at: int = 0

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- 入队 / 排版 / 配图
class ContentCreateRequest(BaseModel):
    """用户点「保存到队列」时提交（对话产出的显式入库动作）。"""

    platform: Literal["gzh", "xiaohongshu", "douyin", "zhihu"]
    title: str
    body: str
    summary: str = ""
    kind: Literal["text", "video"] = "text"
    # 子形态：知乎 answer/article/idea，公众号 article/tietu。后端用
    # normalize_sub_type 收敛：非知乎/公众号归空，知乎传错回落 answer，
    # 公众号传错回落 article —— 不让老客户端/漏传直接 400。
    sub_type: str = ""
    conversation_id: str | None = None
    # 前端消息 id，便于回溯是哪条 AI 回复存下来的
    msg_id: str | None = None
    # 知乎回答要挂在哪个问题下（传问题 URL 或纯 ID 都行，后端统一归一）。
    # 只有 zhihu + answer 有意义；其余情况存了也不展示。
    zhihu_question: str = ""


class ContentUpdateRequest(BaseModel):
    """部分更新内容（加工排版面板里改 Markdown 正文 / 排版 HTML / 标题）。

    全字段可选，None = 不改 —— 区别于「传空串 = 清空」。这样前端可以只传
    真的要改的字段，不会被漏传的字段覆盖成空。
    """

    title: str | None = None
    body: str | None = None
    summary: str | None = None
    body_html: str | None = None


class LayoutRequest(BaseModel):
    theme: str = "moyu-green"
    platform: Literal["gzh", "xiaohongshu", "zhihu"] = "gzh"
    # 排版前是否自动按正文生成一张封面（默认开）。cover 通道不可用时静默跳过。
    auto_cover: bool = True
    # 公众号排版完成后是否自动在文末补 2-3 个精准 #话题#（默认开）。
    # 仅对 gzh 生效；小红书文案已在文末自带话题标签。
    auto_tags: bool = True
    # 作者信息（面板临时覆盖用）。留空 = 排版时不替换 {{作者名}}/{{一句话简介}} 占位。
    author_name: str = ""
    author_intro: str = ""
    # 公众号账号（关注卡用）：留空 = 用默认号。用它替换 {{号名}}/{{slogan}} 并注入二维码。
    account_id: str = ""


class AuthorSettingRequest(BaseModel):
    """作者默认信息（系统设置里配一次，排版时自动替换占位符）。"""

    author_name: str = ""
    author_intro: str = ""


class ChatProviderRequest(BaseModel):
    """对话供应商设置：deepseek（默认，全功能）/ cloudflare（Workers AI）。

    model 仅 cloudflare 时有效：用户在设置页选的 CF 模型 id，
    空 = 用 .env 的 CLOUDFLARE_CHAT_MODEL 兜底。
    """

    provider: Literal["deepseek", "cloudflare"] = "deepseek"
    model: str = ""


class CoverGenRequest(BaseModel):
    """生成封面（模板化，非 AI 生图）。

    全部可省：留空则从正文自动解析标题/副标题，风格/尺寸取 .env 默认值。
    """

    title: str = ""
    sub: str = ""
    tag: str = ""
    style: str = ""  # gradient / dark / light，留空用 COVER_STYLE
    size: str = ""  # wide / square / og，留空用 COVER_SIZE
    date: str = ""
    theme: str = ""  # 排版主题 id，让封面配色跟随该主题（留空用默认品牌色）


class ImageGenRequest(BaseModel):
    prompt: str
    size: str = ""  # 留空则用 settings.IMAGE_SIZE
    count: int = Field(default=1, ge=1, le=4)
    # 插入正文的位置（0=文首，-1=文末，n=第 n 段后）；默认文末
    position: int = -1
    # 生图通道：script（本地脚本）/ cloudflare（Cloudflare Workers AI）。默认 script
    provider: str = "script"


class ImageUpdateRequest(BaseModel):
    alt: str | None = None
    position: int | None = None


class QueueItemOut(BaseModel):
    id: str
    content_id: str
    platform: str
    title: str
    status: str
    checklist: dict[str, Any] | None = None
    wechat_media_id: str | None = None
    # 标题现在还能改吗？规则只有一处（routers/queue.py:_title_editable），
    # 前端不自己判断 —— 免得两边规则漂移，出现「按钮有、点了报错」。
    title_editable: bool = False
    # 关联内容排到哪一步了（none=还没排版）。仅用于展示/排查
    layout_status: str = ""
    # 关联内容的加工中标记，透传给队列页（队列行显示「排版中/生图中」）
    processing: str = ""
    # 来源会话与消息（从 Content join 出来）：队列页据此「回到对话」并定位到那条回复
    conversation_id: str | None = None
    msg_id: str | None = None
    # 子形态（知乎：answer / article / idea；公众号：article / tietu）
    sub_type: str = ""
    # 「去发布」深链。知乎没有开放发布接口（官方 API 只能读），所以系统是
    # 「生成 → 人带走 → 人发布」：这里给出对应形态的网页入口，省得用户自己找。
    # 公众号走草稿箱推送，不需要这个字段（空串）。
    publish_url: str = ""
    created_at: int
    published_at: int | None = None

    model_config = {"from_attributes": True}


class QueueStatusUpdate(BaseModel):
    # 两个字段都可选：可以只改状态，也可以只改标题（改标题时不用硬塞一个状态）
    status: Literal["draft", "ready", "published", "archived"] | None = None
    # 改标题。只有未排版（且没推过草稿箱、没在加工中）的项才让改 ——
    # 排版之后标题已经跟正文/草稿箱绑定了，改了会对不上。
    title: str | None = None


class ChecklistUpdate(BaseModel):
    checklist: dict[str, bool]


# ---------------------------------------------------------------- 公众号账号
class WeChatAccountCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    appid: str = Field(min_length=1, max_length=64)
    secret: str = Field(min_length=1, max_length=256)
    homepage: str = Field(default="", max_length=300)
    gzh_name: str = Field(default="", max_length=120)  # 对外号名（{{号名}}）
    slogan: str = Field(default="", max_length=200)  # 关注卡副标（{{slogan}}）
    is_default: bool = False


class WeChatAccountUpdate(BaseModel):
    """部分更新。secret 留空 = 不改（避免用户改名称时被迫回填密钥）。"""

    name: str | None = None
    appid: str | None = None
    secret: str | None = None
    homepage: str | None = None
    gzh_name: str | None = None
    slogan: str | None = None


class WeChatAccountOut(BaseModel):
    id: str
    name: str
    appid: str
    # 脱敏后的占位，前端只用于判断「有没有配」
    secret_set: bool = False
    homepage: str = ""
    gzh_name: str = ""
    slogan: str = ""
    # 二维码：qrcode_url 可直接用于 <img src>；qrcode_set 供前端判断是否已配
    qrcode_url: str = ""
    qrcode_set: bool = False
    is_default: bool = False
    verified_at: int | None = None
    created_at: int
    updated_at: int = 0

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- 视频
class VisualScene(BaseModel):
    """PPT 模式的单页画面：narration 进配音，slide_image/title/body 上屏。"""

    narration: str = ""
    title: str = ""
    body: list[str] = Field(default_factory=list)
    # LibreOffice 渲染的真实幻灯片图（相对 OUTPUT_DIR），空串表示没有、退回文字卡片
    slide_image: str = ""


class VideoRenderRequest(BaseModel):
    script: str
    ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    voice: str = ""
    subtitle: bool = True
    title: str = Field(default="", max_length=100)
    # PPT 模式：每页标题+要点，随任务存库，渲染时映射到镜头
    visual_scenes: list[VisualScene] | None = None


class JobPatch(BaseModel):
    """历史任务的可改字段：全部可选，传啥改啥。"""

    title: str | None = Field(default=None, max_length=100)
    script: str | None = None
    visual_scenes: list[VisualScene] | None = None
    # 换音色（含换成自己克隆的音色 voice_id），改完点「重新渲染」生效
    voice: str | None = None


class JobOut(BaseModel):
    id: str
    status: str
    progress: int
    stage: str
    output_path: str = ""
    error: str = ""
    title: str = ""
    source: str = "text"
    ratio: str = ""
    voice: str = ""
    output_url: str = ""
    duration_sec: float = 0
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    # 回显用：口播稿与 PPT 每页结构（点击历史任务时回填到编辑区）
    script: str = ""
    visual_scenes: list[VisualScene] | None = None
    created_at: int = 0
    finished_at: int | None = None


class JobProgress(BaseModel):
    """轮询专用的精简返回。

    完整 JobOut 里有 script(约 4KB) 和 visual_scenes(可达 8KB+)，合计十几 KB，
    但轮询只需要「跑到哪了」。拿完整包轮询意味着每 2 秒跨境传十几 KB 无用数据，
    实测经 Cloudflare Tunnel 单次 1.7~2.9 秒，纯浪费。这里只回进度必需字段，
    响应从 22KB 降到约 200 字节。

    前端在结束时再调一次完整 GET /jobs/{id} 拿 script / visual_scenes 回填。
    """

    id: str
    status: str
    progress: int
    stage: str = ""
    error: str = ""
    output_url: str = ""
    output_path: str = ""
    duration_sec: float = 0
    size_bytes: int = 0
    finished_at: int | None = None


class JobPage(BaseModel):
    """任务列表分页结果（分页语义统一用 page / pageSize）。"""

    items: list[JobOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 10
    pages: int = 1


# ---------------------------------------------------------------- 工具
class ToolOut(BaseModel):
    name: str
    description: str
    requires_confirm: bool
