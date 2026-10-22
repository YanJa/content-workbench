# API 接口文档

Base URL：`/api`（前端 axios baseURL 同此）。交互式文档：后端启动后访问 `http://localhost:8000/docs`（Swagger）或 `/redoc`。

所有时间戳均为 Unix 秒。除对话流式接口外，请求/响应均为 JSON。

---

## 认证（用户模块）

用户模块上线后，数据归属由**服务端从登录态解析**（owner = 登录用户 id），客户端传的值不再被信任。

### 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（首用户免邀请码，并认领存量数据；之后按 `AUTH_REGISTER_MODE`） |
| POST | `/api/auth/login` | 登录，返回 JWT |
| GET | `/api/auth/me` | 当前登录用户（前端启动时验证本地 token 是否有效） |

注册请求：`{"username": "alice", "password": "********", "invite_code": ""}`；
登录/注册/me 响应均含 `{"token": "<jwt>", "user": {"id": "...", "username": "...", "created_at": 0}}`（me 只返回 user 字段）。
注册响应额外带 `claimed` 字段（登录恒为 null）：**仅首用户注册时非空**，`{"rows": <接管行数>, "urls": <重签直链数>, "voice_dir_moved": <音色目录是否搬迁>}`，前端据此展示「存量接管摘要」。

### 普通请求鉴权

- 登录后拿 `token`，之后所有请求带 `Authorization: Bearer <token>`（前端 axios 拦截器与 SSE fetch 自动带上，无需手动）；
- token 有效期 7 天（`.env` 的 `JWT_TTL_HOURS`）；缺失 / 无效 / 过期返回 **401**，前端清 token 并跳登录页；
- 未登录访问业务接口 = **401**；访问别人的数据与「数据不存在」同响应 **404**，不泄露存在性。

### 浏览器直链鉴权（`<img>` / `<audio>` / `<video>` 的 src）

直链带不了请求头，URL 由服务端拼成 **HMAC 签名**形式（不过期）：

```
<path>?o=<用户id>&sig=<hmac-sha256(secret, owner + "\n" + path) 取前 32 位 hex>
```

- 签名按「用户 + 路径」绑定：拿到一个 URL 只能读它签名时对应的那一个路径；
- 同源前端 fetch 也可以直接带 `Authorization: Bearer`（无需签名参数）；
- 匿名 / 签名不符 / 签名对应用户不存在 → **404**（与「资源不存在」同响应）。

### 存量认领（首用户注册时，同事务）

单租户 Demo 期的存量数据挂在 `owner='default'` 下。第一个用户注册时同一事务内完成：

1. 所有业务表的 `owner='default'`（含 NULL）改判为该用户 id；
2. 持久化的旧式 `?owner=default` URL 重签为新签名 URL（`contents` 的 `images` / `layouts` / `body_html`）；
3. 音色目录 `data/voices/profiles` → `data/voices/owners/<uid>/profiles`。

认领后旧 URL 全部失效（安全上正确）；WeChat 已转存草稿若引用了旧 URL，需重新排版。

### 注册策略（`.env`）

| `AUTH_REGISTER_MODE` | 行为 |
|---|---|
| `invite`（默认） | 首用户免码；其余账号需填对**邀请码**（`invite_codes` 表，管理员生成；旧的 .env 全局码启动时自动导入为一条不限次记录） |
| `open` | 开放注册 |
| `closed` | 关闭注册 |

- 用户名重复返回 **409**；用户名格式（3~32 位字母数字 `_-`）或密码（≥8 位）不合法返回 **422**；
- 邀请码无效/已用尽/已停用返回 **403**（文案统一「邀请码无效或已用尽」，不泄露码是否存在）；
- 登录按用户名限流：5 次 / 15 分钟，超出返回 **429**；撞邀请码按 IP 限流（防遍历）。

---

## 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 探活（给负载均衡用） |
| GET | `/api/health` | 探活（带前缀版，前端用） |

`/api/health` 响应：

```json
{
  "status": "ok",
  "env": "dev",
  "llm_configured": true,
  "redis": false,
  "tools_enabled": true,
  "model_flash": "deepseek-flash",
  "model_pro": "deepseek-v4-pro",
  "fallback_model": "",
  "cf_chat_model": "@cf/meta/llama-3.1-8b-instruct-fp8",
  "cf_configured": true
}
```

模型字段是**系统级 .env 配置**（探活不查库，保持轻量）：`model_flash` 管文案/轻任务/视觉，`model_pro` 管复杂推理，`fallback_model` 空 = 主模型失败不兜底。**用户当前实际在用的通道与模型**（在个人资料页改的，DB 优先）另见 `GET /api/settings/chat-provider`——「服务健康」卡片的「AI 模型」行是两者拼出来的。

---

## 对话

### POST `/api/chat/stream` — 流式对话（SSE）

请求体：

```json
{
  "conversation_id": "uuid 或 null（null=新建会话）",
  "message": "帮我写一篇公众号文章，主题是秋日咖啡",
  "platform": "gzh | xiaohongshu | douyin | null",
  "topic": "可选，话题约束",
  "audience": "可选，目标人群",
  "enable_tools": true,
  "assistant_msg_id": "可选，前端为这条回复预生成的 id；落库时直接当消息主键（见下）"
}
```

> `assistant_msg_id`：客户端预生成的 uuid。带上后，落库的 assistant 消息主键即此 id，
> 于是「保存到队列」上报的 `msg_id` 与历史消息 id 天然一致——队列页反跳回对话时
> 按 `msg_id` 直接定位那条 AI 回复，零猜测。省略则后端自生成 uuid。

响应是 `text/event-stream`，每个事件一行 `data: {...}\n\n`。事件类型：

| type | 载荷 | 说明 |
|---|---|---|
| `conversation` | `{conversation_id}` | 首个事件，新建会话时前端据此记住 ID |
| `delta` | `{content}` | 增量正文，逐段拼接渲染 |
| `reasoning` | `{content}` | 增量思维链（推理模型才有），前端渲染「思考过程」折叠块 |
| `tool_call` | `{id, name, arguments}` | 模型发起工具调用 |
| `tool_result` | `{id, name, result}` | 工具执行完成（result.error 有值=失败） |
| `tool_confirm` | `{id, name, arguments, message}` | **危险工具等待人工确认**，前端渲染确认卡片 |
| `usage` | `{usage: {prompt_tokens, completion_tokens, total_tokens}}` | token 统计 |
| `done` | `{finish_reason}` | 正常结束 |
| `error` | `{message}` | 出错（含未配 API Key 的降级提示） |

流结束前可能交替出现多个 delta/tool 事件（工具循环最多 5 轮）。

**落库内容（2026-10 起）**：正文、思维链（`reasoning`）、工具痕迹（`tool_calls`）、
token 用量全部落库。历史会话重开时完整回显：思维链默认折叠、工具痕迹附在所属
assistant 消息上（**不落 `role="tool"` 行**——工具执行结果回填进发起调用的那条
assistant 的 `tool_calls[i].result`）。思维链只给人看，`build_context` 不回灌模型
（省 token、避免 provider 兼容问题）。

### POST `/api/chat/confirm` — 确认执行危险工具

用户在确认卡片点了「允许」后调用：

```json
{
  "conversation_id": "可选。对话页确认时传真实会话 uuid（结果回写消息流）；队列页 / 加工面板推送不来自任何对话，**省略该字段即可**（不要传占位串）",
  "tool_call_id": "tool_confirm 事件里的 id（队列页可用 q_{itemId}）",
  "tool_name": "push_wechat_draft",
  "arguments": {
    "title": "标题",
    "html": "<p>正文HTML</p>",
    "content_id": "有则后端会用 content.images 转存正文图、取首图当封面",
    "account_id": "可选，指定推哪个公众号；留空用默认号"
  }
}
```

响应：`{ "ok": true, "result": { ...工具返回值 } }`。

`conversation_id` 语义（易踩坑）：
- **传了** → 服务端会去 `conversations` 表校验其存在性，查不到返回 `404 {"detail":"会话不存在"}`；执行结果**回填进「发起这次调用」的那条 assistant 消息**的 `tool_calls[i].result`（按 `tool_call_id` 定位），**不再另起一行 `role="tool"`**——工具痕迹要跟着所属回复走，历史回显才连得上。
- **省略 / `null` / `""`** → 跳过会话校验、不写回消息流（queue 页与加工面板走此路径）。**切勿把 `conversation_id` 塞成占位字符串**（如旧前端的 `"queue"`），那会被当成真实会话查询而必报 404。
- 无会话时下游 usage 统计与 `Content.conversation_id` 落 `NULL`，属预期。

服务端会校验 tool_name 是否真在 `TOOL_REQUIRE_CONFIRM` 名单里，防止绕过确认机制。
`args.content_id` 是可选增强：带上后后端会读该内容已排版的 `body_html` 与 `images`，把本地图转存进微信素材库、拿首图当草稿封面（`thumb_media_id`）；不带则只按传入的 `html` 纯文本推送。

**推送的硬性前提——封面必填**：微信 `draft/add` 要求每篇草稿必须有封面
（`thumb_media_id` 必填，空串/省略都返回 40007 invalid media_id，2026-10-21 实测）。
没配图时后端直接返回「还没有封面图」+ 加工排版指引，不再把微信的 40007 原样抛出。

**`delete_wechat_draft`（2026-10-21 新增，删除微信草稿箱里的草稿）**：

```json
{
  "tool_call_id": "qd_{itemId}",
  "tool_name": "delete_wechat_draft",
  "arguments": {
    "media_id": "草稿 media_id（推送成功时返回，队列行上带有）",
    "content_id": "可选。带上后删除成功会同步清掉该记录的推送状态",
    "account_id": "可选，指定用哪个公众号的凭证；留空用默认号"
  }
}
```

- 微信侧删除**不可恢复**（草稿箱没有回收站），所以前端有二次确认弹窗。
- 删除成功后，后端把命中行的 `queue_items.wechat_media_id` 置空——本地「已推送」
  标记与推送按钮复位，状态与微信侧保持一致，可重新推送。
- 微信对「已不存在的草稿」执行删除返回 `errcode 0`（幂等），因此用户在后台
  删过的草稿走本工具同样是成功语义，本地陈旧标记一并清掉。
- 该工具权限点 `wechat.push`，**不耗推送配额**（配额只对 `push_wechat_draft` 扣减）。

### 会话 CRUD

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/chat/conversations` | 会话列表（按更新时间倒序） |
| POST | `/api/chat/conversations` | 建会话 `{title?, scene?}` |
| GET | `/api/chat/conversations/{id}` | 会话详情（含全部消息，还原工具轨迹） |
| DELETE | `/api/chat/conversations/{id}` | 删会话（级联删消息） |

会话详情里的消息对象（`MessageOut`）字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `role` / `content` / `created_at` | — | 基础字段（`tool_name` 为历史遗留，可忽略） |
| `prompt_tokens` / `completion_tokens` | int | 该条消息的 token 用量，前端还原用量行 |
| `reasoning` | str | 思维链全文（只给人看，不回灌模型）；空串=普通模型 |
| `tool_calls` | object \| null | 展示型工具痕迹 `{"calls": [{id, name, arguments, result, status}]}`；`status` = `running`/`done`/`error`/`pending_user_confirmation`（pending 时无 `result`） |

---

## 内容与发布队列

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/content/items?platform=&limit=` | 内容成品列表 |
| GET | `/api/content/items/{content_id}` | 单条内容（队列页推草稿箱时取正文；含 `body_html`/`images`/`layouts`/`layout_status` 等加工产物） |
| POST | `/api/content/items` | **建成品并入队**（JSON body，见下）；队列项初始 `status=draft`（待加工） |
| PATCH | `/api/content/items/{content_id}` | **部分更新内容**，body `{title?, body?, summary?, body_html?}`；只更新显式传入的字段（`null`/缺省 = 不改），加工排版面板保存编辑用 |
| GET | `/api/queue/items?status=&platform=&page=&page_size=` | 队列列表（**分页**，见下） |
| GET | `/api/queue/stats` | 统计 `{total, by_status, by_platform}` |
| PATCH | `/api/queue/items/{id}` | 改状态 `{status: draft\|ready\|published\|archived}`，置 published 时自动记 published_at |
| PUT | `/api/queue/items/{id}/checklist` | 保存检查清单 `{checklist: {key: bool}}` |
| DELETE | `/api/queue/items/{id}` | 删除队列项 |
| GET | `/api/queue/export` | 导出 Markdown 发布清单 `{markdown}` |

#### 队列分页

`GET /api/queue/items` **分页返回**，筛选在后端做：

- 查询参数：`status`（`all`/`draft`/`ready`/`published`/`archived`）、`platform`（`all`/`gzh`/…）、
  `page`（默认 1）、`page_size`（默认 20，上限 100）。
- 响应：

```json
{ "items": [ /* 当前页，含透传的 processing / conversation_id / msg_id 字段 */ ],
  "total": 25, "page": 1, "page_size": 20 }
```

> ⚠️ 筛选（`status`/`platform`）必须在**后端**做，前端拿到的是**当前页**，
> 不能再在本地过滤，否则每页条数会对不上。

队列项（`QueueItemOut`）除内容字段外透传两个跳转用字段：
`conversation_id` / `msg_id` —— 该队列项**来源对话**的会话 id 与 AI 回复消息 id
（用户在对话气泡上点「保存到队列」时上报，见下）。队列页据此提供「回到对话」，
跳 `/chat?conversation_id=…&msg_id=…&content_id=…` 反跳回原会话并定位高亮那条回复。

### 保存到队列

对话创作**不再自动入队**（2026-10-16 起）。模型只产出纯内容，用户在对话气泡上点「保存到队列」时前端才调这个接口：

```json
{
  "platform": "gzh",
  "title": "秋日咖啡的三种喝法",
  "body": "# 秋日咖啡…\n> 导语…\n## 一、…",
  "summary": "可选，一句话摘要（推草稿箱时当 digest）",
  "kind": "text",
  "conversation_id": "可选，回填以便队列项跳回原对话",
  "msg_id": "可选，来源消息 ID"
}
```

响应：`{ "ok": true, "content_id": "uuid", "queue_item_id": "uuid" }`。

队列项以 `status="draft"` 建出（= 待加工），到队列页做过排版/配图后才变 `ready`。

### 对话 ⇄ 队列双向跳转（URL 契约）

两个方向的跳转都走 URL query，后端无感，全部由前端消费：

| 方向 | URL | 说明 |
|---|---|---|
| 会话 → 队列 | `/queue?content_id={contentId}` | 对话气泡「保存到队列」/「前往队列」带来源内容 id，队列页定位到该行 |
| 队列 → 会话 | `/chat?conversation_id={convId}&msg_id={msgId}&content_id={contentId}` | 队列页「回到对话」反跳；ChatView 打开来源会话、滚动定位并闪烁高亮 `msg_id` 那条 AI 回复，成功后 `router.replace` 清掉 query（刷新不重复定位），同时显示「← 返回发布队列」闭环条 |

兜底：`conversation_id` 对应的会话已被删除时（接口 404），ChatView 不白屏——
回空会话 + `message.warning('来源会话已不存在，已为你打开新会话')` + 清 query。

### 内容加工（队列 → 可发布稿）

对话产出的是**纯内容**（结构化 Markdown）；排版加工、配图、推草稿箱都在这一层完成。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/content/items/{content_id}/theme-options` | 可选主题 + 能力位 `{themes[], default, gen_ready, cover_ready, cover_styles, cover_sizes, cover_default_style, cover_default_tag}` |
| POST | `/api/content/items/{content_id}/layout` | **提交排版任务**（秒回），body `{theme, platform, auto_cover?, author_name?, author_intro?, account_id?}` → `{ok, task_id, status, poll}`；已在排版中返回 `{ok:false, message}` |
| GET | `/api/content/items/{content_id}/layout/{task_id}` | **轮询排版进度** `{status, progress, stage, result?, error?}`；404=任务中断 |
| POST | `/api/content/items/{content_id}/cancel-processing` | **取消加工中状态**（解卡），→ `{ok, cleared, task_id}` |
| POST | `/api/content/items/{content_id}/covers/generate` | **生成封面**（模板化），body `{title?, sub?, tag?, style?, size?, date?}` |
| POST | `/api/content/items/{content_id}/images/generate` | 调本地生图脚本，body `{prompt, size?, count?, position?}` |
| POST | `/api/content/items/{content_id}/images/upload` | 手动上传配图（multipart `file`），无生图脚本时的兜底路径 |
| PUT | `/api/content/items/{content_id}/images/{image_id}` | 改配图 `{alt?, position?}` |
| DELETE | `/api/content/items/{content_id}/images/{image_id}` | 删配图（连带删文件） |
| GET | `/api/content/images/{content_id}/{filename}` | 配图静态服务（预览 / iframe 排版预览用） |

**封面 vs 文内配图**：两者是独立通道。封面走 `COVER_SCRIPT`（HTML 模板 + 无头浏览器截图，文字确定、品牌统一），请求参数可全部省略 —— 留空则从正文自动解析标题/副标题。产出图**插在 `images` 首位**（`source="cover"`），因此正文里 `<!--IMG:1-->` 天然指向封面；**重复调用 = 换封面**（旧图自动清理）。`cover_ready=false` 时该端点返回 400，前端禁用按钮并引导上传。

`layout` 的 `auto_cover`（默认 `true`）：排版前若还没有封面，自动按正文生成一张并置顶；封面通道不可用时静默跳过，不影响排版。

`layout` 的 `author_name` / `author_intro`（默认空）：排版完成后，在写库前把产出 HTML 里的 `{{作者名…}}` / `{{一句话简介…}}` 替换成这两个值（含组件库里的带说明变体，如 `{{作者名，未知则删整行}}`）。**留空则不替换、保留占位符**。加工排版面板会带出系统设置里的默认值，可临时改这一篇。实现见 `backend/app/services/author.py`。

`layout` 的 `account_id`（默认空 = 默认号）：**关注引导**用。排版时依次做四件事：
① **剥离开头关注卡**（`<!--OPENING_FOLLOW_CARD-->…` 包裹块，本号只在文末放一次）；
② 替换 `{{号名}}`（用该号 `gzh_name`，空则回退 `name`）与 `{{slogan}}`；
③ 把 `{{二维码}}` 换成该号二维码 `<img>`（**未配二维码则整块占位删除，不留空白**）；
④ 替换作者占位符。
替换白名单见 `services/author.py` 的 `_KEYS`（只有 `作者名`/`一句话简介`/`号名`/`slogan` 四个键会被替换，其余 40+ 槽位一律不动）。
⚠️ 只有 `mashang-liaoliao` 主题的组件库含账号占位符与二维码槽位；其它 7 套主题无此机制（不报错，静默跳过）。

**主题**共 8 套：`zhici-qinglv` / `moyu-green`（默认）/ `red-white` / `graphite-minimal` / `zen-whitespace` / `moyu-ticket` / `olive-journal` / `mashang-liaoliao`（码上聊聊，品牌定制）。实际可选列表以 `GET …/theme-options` 返回的 `themes` 为准。

> **为什么排版是异步任务**：公众号排版走 pro 模型装配 + 校验回修，单次实测约 **2~3 分钟**，
> 远超前端 axios 全局 60s 超时 —— 同步接口会让前端「一直转圈然后超时」，而后端其实还在跑，
> 用户过一会儿再打开就发现已排好（这正是 2026-10-16 修复的现象）。
> 现在 `POST …/layout` **毫秒级返回** `task_id`，前端轮询 `GET …/layout/{task_id}` 拿进度与结果。
> 进度阶段：0 排队 → 15 准备组件库 → 30 装配 → 60/70 校验回修 → 90 装订配图 → 100 完成。

`POST …/layout` 响应：

```json
{ "ok": true, "task_id": "3f72f0e5…", "status": "queued", "poll": "/api/content/items/<id>/layout/<task_id>" }
```

`GET …/layout/{task_id}` 响应：

```json
{
  "task_id": "3f72f0e5…",
  "content_id": "ea55a3c0…",
  "status": "running",
  "progress": 30,
  "stage": "AI 正在按主题装配排版…",
  "result": null,
  "error": ""
}
```

`status ∈ queued / running / done / error`；`done` 时 `result` 为：

```json
{
  "ok": true, "theme": "moyu-green", "theme_name": "摸鱼绿",
  "html_len": 15127, "errors": [], "warnings": [], "layout_status": "ready"
}
```

`platform="gzh"` 时产出公众号 HTML（写回 `content.body_html`，`layout_status` 置 `ready`，无 errors 时队列项 `draft → ready`）；`platform="xiaohongshu"` 时只做纯文本规整，结果存 `content.layouts["xiaohongshu"]["text"]`。`errors` 非空表示 LLM 装配后仍不满足硬规则（回修 ≤2 轮）。

> 任务表存在**进程内存**里：服务重启会丢，此时轮询返回 **404**，前端提示「任务已中断，请重新排版」。
> 排版是幂等的，重跑一次即可（代价只是一次 LLM 调用），所以不落库。

#### 加工中标记与「取消」（解卡）

`Content.processing`（`""` / `layout` / `image`）是**库里的长任务互斥标记**，队列页据此禁用
该行的所有操作按钮。它有三条「解除」路径，缺一不可：

1. **任务正常/异常结束** —— worker 的 `finally` 清空（正常路径）；
2. **服务启动时** —— `recover_stuck_processing()` 把残留的非空标记统一清空
   （进程重启/沙箱休眠时正在跑的任务没了，标记会永久残留、把按钮永久锁死）；
3. **手动取消** —— `POST /api/content/items/{id}/cancel-processing`，清标记并把内存里
   仍 `running` 的排版任务标记为「已取消」，让前台停止等待。用户中途关面板也会自动触发。

响应：`{ "ok": true, "cleared": "layout", "task_id": "" }`（`cleared` 是清除前的标记）。

> 说明：排版协程在串行锁里跑，**无法从外部真正杀掉**；「取消」保证的是「标记解除 +
> 前台停止等待」，让用户能重新操作。

`gen_ready=false` 表示后端未配置生图脚本（`IMAGE_SCRIPT`），此时 `/images/generate` 返回 400，改走 `/images/upload`。
`cover_ready=false` 表示封面通道不可用（`COVER_SCRIPT` 指向的脚本不存在，或本机没装 Chrome/Chromium），此时 `/covers/generate` 返回 400，且排版不再自动带封面。

### 典型工作流

```
对话创作（纯内容 Markdown）
   → 点「保存到队列」→ POST /api/content/items（队列项 = draft）
   → 队列页「加工排版」→ GET theme-options → POST layout（auto_cover 默认开，自动出封面）
   → [可选] 换封面 / 生成·上传文内配图 → POST covers/generate | images/generate | images/upload
     → 再 layout 一次注入 <img>
   → 预览确认（可全屏 / 手机·平板·桌面三档）→「推公众号草稿箱」（走 /api/chat/confirm 确认通道）
   → 到公众号后台草稿箱检查，无误再群发
```

对话页「保存到队列」成功后，气泡操作栏会多出「去队列查看」按钮，点击跳到
`/queue?content_id=<id>`，队列页会自动滚动并高亮该条目 2.5 秒。

---

## 工具与视频

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tools` | 工具清单 `[{name, description, requires_confirm}]` |
| POST | `/api/video/render` | 提交渲染 `{script, ratio, voice, subtitle, visual_scenes?}`；`visual_scenes`（PPT 模式）为每页 `{narration, title, body[]}`，narration 进配音、title/body 上屏；`subtitle`（默认 `true`）把口播稿封装成**内嵌软字幕轨**，画面不受影响 |
| GET | `/api/video/jobs` | 任务列表（落库持久化） |
| GET | `/api/video/jobs/{job_id}` | 查渲染进度（**完整详情**，含 script / visual_scenes） |
| GET | `/api/video/jobs/{job_id}/progress` | **轮询专用精简进度**（约 200 字节，见下） |
| POST | `/api/video/jobs/{job_id}/retry` | 失败/完成沿原参数重跑 |
| DELETE | `/api/video/jobs/{job_id}` | 删任务（连带清成片文件） |
| POST | `/api/video/ppt/parse` | 上传 pptx，解析出 `{script, slides[], visual_scenes[]}`（multipart `file`）；visual_scenes 随渲染提交后画面展示每页标题+要点 |
| GET | `/api/video/outputs/{filename}` | 成片文件（preview / download，支持 Range） |

> **轮询为什么单独开一个接口**
>
> 完整 `/jobs/{id}` 响应约 **22KB**，其中 `script`(约 4KB) 和 `visual_scenes`(可达 8KB+)
> 是回填编辑区才需要的，轮询时完全用不到。经 Cloudflare Tunnel 访问单次实测
> **1.7~2.9 秒**（跨境 + 反向隧道，当时还叠了 nginx basic auth，现已摘除），
> 每 2 秒传一次 22KB 纯浪费。
>
> `/jobs/{id}/progress` 只回进度必需字段（`id/status/progress/stage/error/
> output_url/output_path/duration_sec/size_bytes/finished_at`），实测
> **267 字节**，缩减 98.8%。
>
> 前端配合两点：**自适应间隔**（有变化 4s，无变化退避到 8s→15s）+
> **结束时取一次完整详情**回填 script / visual_scenes。

### 技能包（Skill）

用户私有的「教 AI 怎么做」的模块。一个技能 = 一份 `SKILL.md`：YAML frontmatter
（`name` + `description`）+ 正文步骤。**渐进式披露**：平时只把 `name` + `description`
作为「清单」以独立 system 消息注入（约 50-100 tokens/条），模型判断任务匹配时
才调 `activate_skill` 取回正文原文（见 `services/skill.py`）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/skills` | 登录即可 | 全部技能（含 `invalid` 的，`error` 里是原因） |
| GET | `/api/skills/meta` | 登录即可 | 能力开关：`{enabled, catalog_max, body_max_chars}` |
| POST | `/api/skills` | skill.manage | 粘贴/手填：`{text}`（完整 SKILL.md）。同名覆盖 |
| POST | `/api/skills/upload` | skill.manage | 上传 `.md` 或技能 **zip 包**（包内 SKILL.md 为准，其余文本文件存入 files 随技能存档；条目≤30、单文件≤200KB、解压总量≤1MB，路径穿越/二进制跳过） |
| PATCH | `/api/skills/{id}` | skill.manage | `{enabled?}` 启停 / `{name?}` 改名 |
| DELETE | `/api/skills/{id}` | skill.manage | 删除 |

### 校验规则（来自 Agent Skills 开放规范）

- `name`：1-64 字符，**只允许小写字母、数字、连字符**，不能以连字符开头/结尾、不能连续两个连字符；
- `name` 必须**与文件名（去扩展名）一致** —— 等价于官方的「name 必须等于父目录名」。
  叫 `SKILL.md` 时豁免（官方包的文件名固定是这个）；
- `description`：1-1024 字符，多行会被折叠成一行（catalog 里每条占一行）；
- 正文必填，上限 `SKILL_BODY_MAX_CHARS`（默认 20000 字符）。

### 落库语义：坏文件不会打坏好技能

| 情况 | 结果 |
|---|---|
| 解析成功 | 覆盖同名旧版本，`status=ready` |
| 解析失败，**同名已有可用版本** | **不落库**，返回 400「格式有误，原技能未改动」 |
| 解析失败，没有同名可用版本 | 落成 `status=invalid`，`error` 写明原因（让用户看到为什么没生效） |

第三条是 2026-10-22 实测踩到的坑：原本传对了一个技能，再传一个「文件名与 name
不符」的坏文件，好技能直接被覆盖成 invalid、在用户面前凭空消失且无法撤销。

### 与对话链路的接线

- `chat.py` 在 `build_context` 前调 `skill.build_catalog_for_owner(db, owner)`，
  catalog 作为**独立 system 消息**注入（和知识库同理：`strip_tool_discipline()`
  会把拼在 system_prompt 尾部的内容整段切掉）；
- 对话请求带 `use_skills`（默认 true，与 `use_knowledge` 同款）。为 false 时**既不
  注入 catalog、也不提供 `activate_skill` 工具** —— 只关一半的话，模型会凭工具
  description 去猜一个技能名调用。前端输入框上方有对应开关；
- 回复上方的工具痕迹里，`activate_skill` 单独渲染成「技能 · 按「X」的说明来做」，
  不显示原始 JSON —— 「这轮 AI 读了哪个技能」是用户排查技能不生效时唯一能看的线索；
- **Cloudflare 通道不注入**：CF 模型没有 function calling，清单里「调用
  activate_skill」的指令只会让它编造工具调用；
- `activate_skill` 是**动态工具**（`dynamic_tool`）：`name` 参数的 enum 按当前
  owner 可用集合生成，**没有可用技能时整个工具不提供给模型**（空 enum 会让模型反复试错）。

## 声音复刻

上传一段自己的录音 → 后台复刻出专属音色 → 渲染时把它当 `voice` 传进去。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/video/voices/provider` | 当前复刻后端能力 `{provider, xtts_installed, cloud_ready, available, note}` |
| GET | `/api/video/voices` | 已复刻音色 `{items[], provider}`，item 带 `value`（即 `clone_<id>`）与 `sample_url` |
| POST | `/api/video/voices/clone` | 上传录音（multipart `file` + `name`），立刻返回 `{ok, id, name}`，后台异步复刻 |
| GET | `/api/video/voices/clone/{id}` | 复刻进度 `{status, progress, stage, error?, quality?}`，status ∈ queued/running/done/failed |
| GET | `/api/video/voices/{id}/sample` | 试听（返回参考音频） |
| PUT | `/api/video/voices/{id}` | 改名 `{name}` |
| DELETE | `/api/video/voices/{id}` | 删除音色 |

复刻完成后，`/api/video/render` 的 `voice` 填 `clone_<id>` 即可用你的声音配音：

```json
{ "script": "…", "ratio": "9:16", "voice": "clone_ab12cd34ef56" }
```

三条合成路线由 `.env` 的 `VC_PROVIDER` 决定，详见下方「声音复刻」章节。

`/api/video/render` 响应：

```json
{ "ok": true, "job_id": "uuid", "status": "queued", "message": "...", "poll": "/api/video/jobs/..." }
```

`/api/video/jobs/{job_id}` 响应：

```json
{
  "id": "...", "status": "done", "progress": 100, "stage": "完成",
  "title": "口播稿前30字", "source": "text", "ratio": "9:16", "voice": "zh-CN-XiaoxiaoNeural",
  "output_url": "/api/video/outputs/<job_id>.mp4",
  "duration_sec": 19.77, "width": 720, "height": 1280, "size_bytes": 1967425,
  "created_at": 1792046127, "finished_at": 1792046133, "error": ""
}
```

status 流转：`queued → running → done | failed`。渲染是真渲染（本地 ffmpeg，
非占位）：配音阶段用 edge-tts 并收集 SentenceBoundary 当字幕轴，画面是本地
PIL 渐变卡片 + Ken Burns 运镜，整段约 720p、10 秒成片耗时约 3-6 秒。
任务落 SQLite，服务重启后中断的任务统一标为 failed 并提示「点重试可重新渲染」。

### 口播稿 → 内嵌字幕（软字幕轨）

`subtitle: true`（默认开）时，渲染最后一步会把口播稿写成 SRT，
用 `mov_text` 封装成 mp4 的**第二条流（字幕轨）**：

```
口播稿逐句 → 每句 {text, start, end}（时间轴来自配音实测时长）
           → SRT 文件（HH:MM:SS,mmm）
           → ffmpeg -c:s mov_text -metadata:s:s:0 language=chi
           → 成片.mp4（stream 0 = h264 视频，stream 1 = mov_text 字幕）
```

几条设计取舍：

- **画面原封不动**。字幕是独立轨，不是烧进像素的硬字幕 —— 播放器里可开关、
  可导出、可改字体大小，PPT 页面内容一个像素都不动。
- **按句显示**。每句起止时间取自配音的实际时长（edge-tts 直接拿
  SentenceBoundary；复刻音色按 ≤80 字分段 `ffprobe` 实测累加），不是估算的字级。
- **失败自动降级**。`concat` 时先试 `-c copy`（秒级），失败再退到 filter 重编码，
  两条路径都带字幕轨，不会因为封装失败丢字幕。
- **空句跳过**。口播稿里的空行/纯空白不进字幕，避免空条目。

验证成片有没有字幕轨：

```bash
ffprobe -v error -show_entries stream=index,codec_type,codec_name -of csv=p=0 out.mp4
# 0,h264,video
# 1,mov_text,subtitle      ← 有这行就对了

# 把字幕导出来核对内容与时间轴
ffmpeg -y -i out.mp4 -map 0:s:0 extracted.srt
```

---

## AI 用量

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/usage/summary?days=&conversation_id=&start=&end=` | 总量 + 按天/按模型/按场景三条序列 |
| GET | `/api/usage/records?limit=&offset=&days=&model=&scene=&conversation_id=` | 明细分页 `{items,total,limit,offset}` |
| GET | `/api/usage/top-conversations?days=&limit=` | 消耗最高的会话（带标题） |
| GET | `/api/usage/pricing` | 当前单价表 `{items, state}` |
| POST | `/api/usage/pricing/reload` | 热加载 `config/pricing.json`，不用重启 |

`/api/usage/summary` 响应（节选）：

```json
{
  "total": { "calls": 3, "prompt_tokens": 7362, "completion_tokens": 2451, "reasoning_tokens": 0, "cached_tokens": 512, "total_tokens": 9813, "cost": 0.030367, "avg_latency_ms": 796 },
  "by_day": [ { "key": "2026-10-15", "calls": 2, "cost": 0.028719, "...": "..." } ],
  "by_model": [ { "key": "deepseek-flash", "label": "DeepSeek Flash", "known": true, "priced_as": "deepseek-flash", "cost": 0.03, "...": "..." } ],
  "by_scene": [ { "key": "gzh", "cost": 0.001313, "...": "..." } ]
}
```

`by_model.known=false` 表示该模型没登记单价，按兜底模型（`deepseek-flash`）估算，前端要显式提示。新增模型的单价写在 `backend/config/pricing.json`（格式见 `pricing.example.json`），调 `/api/usage/pricing/reload` 生效。

---

## 收益看板

流量主广告数据来自微信 `publisher/stat`（广告分析）与 `publisher_settlement`（结算），
按 owner 隔离——每个用户只看到自己公众号（`wechat_accounts.owner`）的收益。
天级数据落库 `wechat_ad_daily`：T+1 才出，没必要每次现拉；90 天整段重拉既慢又易限频。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/revenue/accounts` | 当前用户可见的公众号列表 |
| GET | `/api/revenue/overview?account_id=&start=&end=&refresh=` | 汇总 + 收益趋势 + 收益构成；支持多账号聚合，`refresh=1` 强制重拉微信 |
| GET | `/api/revenue/settlement?account_id=&start=&end=` | 结算口径：累计收入 / 已结算 / 扣罚 + 月度结算单 |
| GET | `/api/revenue/export?account_id=&start=&end=&format=xlsx\|csv` | 导出收益数据（文件下载） |

口径说明（前端页面已如实标注，本模块**不作为对账依据**）：

- **统计收益** = 所选区间内每日明细合计（本期口径）；
- **累计收入 / 已结算 / 扣罚** = 微信结算口径，**累计值**，与上方时间筛选无关；
- 待结算 = 累计收入 − 已结算 − 扣罚（估算）；两者有 T+1 与月结时滞，微信自身两口径可能差 1 分。
- **已结算 ≠ 已到账**：微信流量主结算分四步（`sett_status`：1=结算中 → 2/3=已结算 → 4=付款中，后台另有终态「已付款」）。
  `settlement_list` 原样透传微信字段（`zone` 结算区间 / `month` / `order` 1=上半月 2=下半月 / `sett_status` / `settled_revenue` / `sett_no` 结算单号 / `mail_send_cnt` 邮件发送次数）。
  打款规则：次月 1 日与 15 日前分别结算上下半月，结算单出具后 15 个工作日内打款（代扣个税、增值税及附加）；`publisher_payment`（打款记录）个人订阅号不支持（45010），故「已付款」以银行卡实际入账为准。

`/api/revenue/export` 只导**库内已缓存**的数据（不打微信——导出是高频可重试操作，
不该触发限频）；区间没数据时返回 `404`，文案引导用户先在看板页加载/刷新。

- `format=xlsx`：四张表——**收益明细**（天 × 广告位）/ **每日汇总**（趋势同源）/ **收益构成**（按广告位）/ **说明**（口径 + 导出时间）；
- `format=csv`：仅明细，UTF-8 带 BOM，Excel 双击打开不乱码；
- 响应 `Content-Disposition` 带中文文件名（RFC 5987 `filename*`），如 `流量主收益_2026-09-22_2026-10-21.xlsx`。

---

## 知乎热榜

热榜由**后台定时任务**每半小时抓一次落库（`services/zhihu.py` 的 `loop_hot_fetch`，挂 lifespan），
对话页「不知道写什么」时从本地库读给用户看。**用户浏览不消耗知乎额度**，额度只在抓取时花。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/zhihu/hot?limit=&sort=trend\|rank` | 登录即可 | 当前热榜 + 趋势信号，读本地库 |
| POST | `/api/zhihu/refresh` | 仅管理员 | 手动抓一次（`force`，跳水位保护），**消耗 1 次共享额度** |
| GET | `/api/zhihu/status` | 仅管理员 | 连通性 + 额度自查（会消耗 1 次额度） |
| GET | `/api/zhihu/usage` | 仅管理员 | 后台「知乎热点」卡片数据源，**只读本地库不花钱** |
| PUT | `/api/zhihu/secret` | 仅管理员 | 在线设置 Access Secret，保存即生效（免重启），顺手抓一次验票 |

`GET /api/zhihu/hot` 响应（节选）：

```json
{
  "ok": true,
  "fetched_at": 1792419858,
  "interval_minutes": 30,
  "items": [
    {
      "question_id": "question_525603632",
      "title": "能不能建议修改火车开车前五分钟停止检票？",
      "url": "https://www.zhihu.com/question/525603632",
      "summary": "……平台白送的约 200 字摘要，可能为 null……",
      "thumbnail_url": null,
      "rank": 8,
      "climb": 4,
      "is_new": true,
      "staying_hours": 0.3,
      "seen_times": 1,
      "heat_score": 0.533
    }
  ]
}
```

字段口径（前端**只展示、不复刻计算**）：

- `climb`：与上一次成功抓取的名次差，正数 = 在往上冲；`is_new`：今天首次进榜；`staying_hours`：首次进榜距今小时数（越大越是长线议题，不抢时效）；
- `heat_score = 爬升分*0.6 + 名次分*0.4`（默认 `sort=trend` 按它排）——「正在冲」的权重高于「名次高」，因为后者用户自己看榜单就知道；
- `ok=false` 时响应带 `configured`（是否配了 `ZHIHU_ACCESS_SECRET`）与 `error`，前端据此显示「未配置 / 准备中」弱提示，不当成故障弹错。

`GET /api/zhihu/usage` 响应（管理后台「知乎热点」卡片，本地数据零消耗）：

```json
{
  "ok": true,
  "configured": true,
  "masked": "****aaea",
  "used": 6,
  "quota": 100,
  "safe_limit": 88,
  "safe_to_fetch": true,
  "remaining": 94,
  "interval_minutes": 30,
  "last_fetch_at": 1792420360,
  "last_ok": true,
  "last_item_count": 30
}
```

`PUT /api/zhihu/secret` 请求体 `{"secret": "……"}`（**留空 = 清除凭证**），响应同 `usage` 结构外加 `fetch` 字段（保存后立即抓取的结果：`{"ok": true, "saved": 30}`）。要点：

- **DB 优先**：保存的值落 `app_settings` 表（`owner=system`），启动时加载覆盖 `.env`；后台保存过之后 `.env` 的 `ZHIHU_ACCESS_SECRET` 就不再生效，直到后台清空该行；
- 明文只进不出：响应只回 `masked`（后 4 位），请求体的 secret 永不明文回传；
- 保存即生效不用重启：内存 `settings` 实例直接换值，已待命的抓取循环 5 分钟内自动感知启动。

---

## 知识库（RAG 素材）

用户私有的「给 AI 用的素材库」：素材入库后自动切分 + 向量化（Cloudflare bge-m3），
对话写稿时按语义召回相关片段、以独立 system 消息注入上下文（见 `services/knowledge.py`）。
向量化不可用时检索自动降级为关键词匹配，**绝不阻塞对话**。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/knowledge/stats` | 登录即可 | 概览：条数 / 片数 / 就绪片数 / 向量化通道是否就绪 |
| GET | `/api/knowledge/items?q=&source=&status=&page=&page_size=` | 登录即可 | 素材列表（分页 + 搜索 + 筛选，全在后端做） |
| POST | `/api/knowledge/items` | knowledge.create | 新增：`{title?, text}` 或 `{title?, content_id}`（复用成稿，取 Content.body） |
| GET | `/api/knowledge/items/{id}` | 登录即可 | 详情：含 `raw_text` 与切片明细 |
| PATCH | `/api/knowledge/items/{id}` | knowledge.delete | 改标题（内容不支持改——改了等于重切重算） |
| DELETE | `/api/knowledge/items/{id}` | knowledge.delete | 删素材并级联删片段 |
| POST | `/api/knowledge/items/{id}/retry` | knowledge.create | 重跑失败/中断的片段 |
| POST | `/api/knowledge/search` | 登录即可 | 召回调试：`{query, top_k}` → 命中片段 + 分数 + 匹配方式 |

注：对话请求 `POST /api/chat/stream` 的 body 新增 `use_knowledge`（默认 true），
为 false 时该轮完全跳过召回。

`GET /api/knowledge/stats` 响应：

```json
{
  "items": 1, "chunks": 1, "ready_chunks": 1, "pending_chunks": 0,
  "items_limit": 300, "chunks_limit": 3000,
  "vector_ready": true, "embed_model": "@cf/baai/bge-m3"
}
```

`POST /api/knowledge/search` 响应（`match=vector` 为语义命中，`keyword` 为降级命中；
`score` 两者量纲不同——余弦 0~1，关键词是 query 二字组命中比例）：

```json
{
  "query": "买了之后多久能退款",
  "total": 1,
  "hits": [{"item_id": "…", "title": "产品FAQ", "content": "…前300字", "score": 0.7847, "match": "vector"}]
}
```

## 声音复刻

「提取我的声音」= 声音复刻（voice cloning）。用户传一段自己的录音，系统抽取出
音色，之后渲染视频时就能用**他自己的声音**念任何稿子。

### 三条路线（`.env` 的 `VC_PROVIDER` 决定）

| 取值 | 说明 | 依赖 | 效果 | 成本 |
|---|---|---|---|---|
| `edge`（默认） | 不复刻，直接用 edge-tts 官方音色 | 无 | — | 0 |
| `xtts` | 本地 Coqui **XTTS-v2** 零样本克隆 | `pip install coqui-tts` | 良好 | 0（CPU 合成，首次下 ~1.8GB 模型） |
| `cloud` | 云端复刻 API（阿里云 CosyVoice / MiniMax / 火山方舟） | `VC_CLOUD_*` 三项 | 最好 | 按量计费 |

```bash
# 本地零成本开启（推荐先用这条跑通）
cd backend && pip install coqui-tts
# 然后把 .env 的 VC_PROVIDER 改成 xtts，重启后端
```

### 为什么本地路线用 XTTS 而不是"训练一个模型"

XTTS-v2 是**零样本克隆**：给它一段 10~30 秒参考音频，它当场就能用那个音色合成。
不需要 GPU 训练、不需要几小时的训练队列、不需要保存几 GB 的权重。所谓「训练」
在本项目里只是**预热**：加载模型 + 用参考音频算一次 embedding 并缓存，
把几十秒的模型加载时间挪出渲染流程，避免用户看到「渲染卡在 5%」。

### 录音处理管线（决定最终像不像）

用户的录音质量参差不齐，直接丢给模型必然翻车，所以上传后强制过一遍：

1. **重采样** 单声道 24kHz（XTTS 输入规格）；
2. **highpass=60Hz** 滤掉空调/电流低频嗡声；
3. **首尾静音裁剪**（不去中间停顿，那是自然语流的一部分）；
4. **loudnorm** 响度归一到 -18 LUFS，避免音量差异带偏音色 embedding。

处理完还会算一个**声学指纹**（时长 / RMS / 过零率 / 峰值）给用户一个量化反馈
—— 录音太短、太轻、爆音都会明确告知，而不是等复刻完发现不像。

### 合成与时间轴

复刻音色（`voice = clone_<id>`）走 `app/services/voices.py`，在
`video_pipeline.synth_voice()` 里作为独立分支：

```
文本 → 按 ≤80 字切段 → 逐段合成 → ffmpeg concat 拼成一条 mp3
                                                ↓
                     每段 ffprobe 实测时长累加 → 句级时间轴（字幕轴）
```

> **为什么按 80 字切**：本地/云端合成都不返回像 edge-tts 那样的
> SentenceBoundary，时间轴只能按段实测累加。段越长累积漂移越大；80 字一段
> 约 15 秒，漂移在百毫秒级，画面切换看不出来。另外短段还能避免 XTTS
> 在长文本上「念到后面音色跑偏」这个已知毛病。

### 接口速查

```bash
# 1. 看当前能不能克隆
curl localhost:8000/api/video/voices/provider

# 2. 上传录音（10~30 秒，wav/mp3/m4a…）
curl -F "file=@my_voice.m4a" -F "name=我的声音" \
     localhost:8000/api/video/voices/clone
# → {"ok":true,"id":"ab12cd34ef56","name":"我的声音"}

# 3. 轮询直到 done
curl localhost:8000/api/video/voices/clone/ab12cd34ef56

# 4. 用复刻音色渲染
curl -X POST localhost:8000/api/video/render -H 'Content-Type: application/json' \
  -d '{"script":"大家好，这是我自己的声音。","ratio":"9:16","voice":"clone_ab12cd34ef56"}'
```

---

## 设置（公众号管理）

微信公众号凭证既可以写在 `.env`（`WECHAT_APPID`/`WECHAT_SECRET`），也可以在「系统状态」页
添加（落库 `wechat_accounts` 表，支持多个号 + 选默认）。推送时的凭证优先级：
**指定 account_id → 默认号 → `.env` 回退**。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings/wechat` | 公众号列表（默认号排最前；secret 不回传明文，只给 `secret_set`） |
| POST | `/api/settings/wechat` | 新增 `{name, appid, secret, homepage?, gzh_name?, slogan?, is_default?}` |
| PUT | `/api/settings/wechat/{id}` | 改；`secret` 留空 = 不改（改名称时不必回填密钥） |
| DELETE | `/api/settings/wechat/{id}` | 删除（删的是默认号则自动把剩下的第一个提为默认） |
| POST | `/api/settings/wechat/{id}/check` | 测试连通性（拉 access_token），成功回写 `verified_at` |
| POST | `/api/settings/wechat/{id}/default` | 设为默认推送号（默认号全局唯一） |
| POST | `/api/settings/wechat/{id}/qrcode` | 上传公众号二维码（multipart `file`，仅 png/jpg/webp）；换图自动删旧文件 |
| GET | `/api/settings/wechat/qrcodes/{account_id}/{filename}` | 二维码静态服务（**须带 `?owner=`**，跨租户 404） |
| GET | `/api/settings/author` | 取作者默认信息 `{author_name, author_intro}`（未配置返回空串） |
| PUT | `/api/settings/author` | 保存作者默认信息 `{author_name, author_intro}`，排版时用于替换占位符 |
| GET | `/api/settings/chat-provider` | 取对话供应商 `{provider: "deepseek"\|"cloudflare", model, cloudflare_ready}`；未配置返回 `deepseek` |
| PUT | `/api/settings/chat-provider` | 保存对话供应商 `{provider, model}`；`model` 仅 cloudflare 有效，选了清单外模型 400（清单接口不可用时放宽为非空即可）；切回 deepseek 自动清空 model |
| GET | `/api/settings/cloudflare-models` | Cloudflare 可用对话模型清单 `{models: [{name, label}]}`（TTL 缓存 10 分钟）；未配凭证 400，拉取失败 502 |

**对话供应商（`chat-provider`）**：`deepseek`（默认）全功能——思维链、工具调用、精确计费；
`cloudflare`（Workers AI）免费额度大，但**模型不支持 function calling**：切过去后对话内的
工具（查队列/推草稿箱等）自动停用，历史消息里的工具痕迹会在发给模型前剥离，system prompt
里的「工具使用纪律」段也会剥掉；流式响应不带 token 用量，用量页按「估价」展示。只影响
对话轮次——摘要压缩、排版、话题标签等后台任务固定走 DeepSeek。CF 凭证与生图通道共用
`.env` 里的 `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`（Workers AI 权限两者通用）。

**关注引导字段**（`WeChatAccountOut`）：除原有字段外新增
`gzh_name`（对外号名，替换 `{{号名}}`）、`slogan`（关注卡副标，替换 `{{slogan}}`）、
`qrcode_url`（二维码直链，可直接用于 `<img src>`，未配为空串）、`qrcode_set`（布尔）。

**二维码上传为何是独立端点**：二维码属于**账号**、跨文章复用；若复用内容图片上传会被塞进
`Content.images` → 被列进排版提示词、被 `<!--IMG:n-->` 索引，污染内容图片体系。
落盘位置 `data/uploads/qrcodes/{account_id}/`。
**新增账号时还没有 id，需先保存账号再上传二维码**。

`POST …/check` 响应：`{ok: true}` 或 `{ok: false, errcode, error}`，`error` 已按错误码翻译成中文
（如 `errcode=40164` → 「调用来源 IP 不在白名单…」）。另外**总会附带 `egress` 字段**，
报告该次请求的实际出口 IP，便于直接抄进白名单：

```json
{ "ok": false, "errcode": 40164, "error": "调用来源 IP 不在白名单…",
  "egress": { "ok": true, "ip": "49.233.223.252", "via_proxy": true } }
```

> **⚠️ IP 白名单要填「微信实际出网 IP」**
>
> 后端调 `api.weixin.qq.com` 是从**它所在机器**出网的，微信只认这个出网 IP。
> SSH 反向隧道（如 `49.233.223.252:18080`）只负责把公网访问**转发进来**，
> **不承载出网流量**。
>
> **沙箱出网 IP 是动态池**（一次采样能探到 20+ 个 IP），逐个加白不可行。
> 解法：让**仅微信请求**走一台固定公网 IP 的机器出网 —— 设
> `WECHAT_PROXY=socks5://127.0.0.1:10800`（配合 `deploy/proxy-guard.sh` 隧道），
> 此时 `egress.ip` 会显示那台固定机器的 IP，把它填进白名单即可（只需 1 个 IP）。
> 详见架构文档「固定出口代理」一节。
>
> 验证方式：在设置页点「测试连通」，看返回的 `egress.ip`；若报 `40164` 就是该 IP 不在白名单。

---

## 用户管理（管理员，RBAC）

角色两级：`admin` / `member`。**管理员账号由系统启动时预置**
（`core/rbac.py: ensure_admin_account()`：`ADMIN_USERNAME` 指定的账号不存在就创建、
存在只把角色摆正，绝不动密码；初始密码取 `ADMIN_INITIAL_PASSWORD`，没配则随机生成并打进启动日志）。
注册进来的一律 member。管理员的「用户管理」页对应本组端点，**全部要求 admin 角色**，非管理员 403。

`/api/auth/*` 的 `UserOut` 在原 `id/username/created_at` 之上新增：

| 字段 | 说明 |
|---|---|
| `role` | `"admin"` 或 `"member"`；前端据此决定菜单显隐 |
| `is_active` | 停用后所有 Bearer 请求当场 401（角色/停用都不进 JWT，改完立即生效） |
| `permissions` | 生效的权限点 key 列表（按钮级权限，前端显隐/置灰的唯一依据） |
| `quotas` | 生效的配额配置 `{feature: {period, limit}}`，`limit=null` 表示不限 |
| `usage` | 本周期已用量 `{feature: {used, limit, left, unlimited, reset_at}}` |
| `invite_code` | 注册时用的邀请码（渠道统计）；空 = 首用户 / 代建 / 预置 admin |

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/users` | 全部用户，含 `last_login_at`、`contents`、`permissions`、`quotas`、`usage`、`invite_code`、`invite_note`（渠道备注） |
| POST | `/api/admin/users` | 代建账号 `{username, password, role?}`，**绕过邀请码**；用户名冲突 409、密码不足 8 位 422 |
| PATCH | `/api/admin/users/{id}` | 改角色 / 停用 `{role?, is_active?}`；两条 400：动自己的账号、把最后一个管理员降级/停用；非法 role 也是 400 |
| GET | `/api/admin/stats` | 全站聚合：用户数、内容/队列/渲染数、LLM 总调用与花费、`by_user` 每人明细 |

**权限与配额（按钮级，2026-10 上线）**：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/permissions` | 权限点目录 + 各角色当前模板（`permissions` 为 `null` = 全部开放且固定） |
| PUT | `/api/admin/role-permissions/{role}` | 改角色权限清单/配额；admin 传 permissions → 400（不可收窄） |
| PUT | `/api/admin/users/{id}/overrides` | 个人增量覆盖 `{perm_override: {grant, deny}, quota_override}` |
| POST | `/api/admin/users/{id}/quota-reset` | 重置用量计数（只清计数不清配置），`feature` 缺省 = 全部 |

**邀请码管理（2026-10 上线）**：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/invite-codes` | 全部邀请码（最新在前），含 `code/note/max_uses/used/disabled/exhausted` |
| POST | `/api/admin/invite-codes` | 生成 `{code?, note?, max_uses?}`；code 留空随机，max_uses 留空/0/负 = 不限；自定义码冲突 409 |
| POST | `/api/admin/invite-codes/{id}/disable` | 停用（立即不能再注册） |
| POST | `/api/admin/invite-codes/{id}/enable` | 重新启用 |
| DELETE | `/api/admin/invite-codes/{id}` | 删除（仅 `used=0` 的码；用过的码 400，改停用） |

**刻意没有 DELETE（用户）**：删除 = 停用（保留数据与可恢复性）。真删会让该用户历史配图集体 404
（存量直链签名绑 `?o=<uid>`，已写进正文 HTML）。

**管理员看不到别人的内容** —— 业务端点仍按 owner 隔离，管理面只有账号操作与聚合数字。

---

## 错误约定

- 业务错误：`{"detail": "中文说明"}` + 合适的 4xx（404 不存在或无权限 / 422 参数问题）；
- 未登录 / 登录过期 = **401**（前端自动跳登录页）；账号被停用也是 **401**（文案区分：「账号已被停用，请联系管理员」）；
  登录失败超限 = **429**；用户名冲突 = **409**；非管理员调管理端点 = **403**；
- 直链端点（配图 / 成片 / 试听 / 二维码 / 幻灯片图）对匿名与越权一律 **404**，与「资源不存在」同响应；
- 工具内部失败不抛 HTTP 错误，而是 `result.error` 带回，前端按失败标记渲染；
- AI 未配置 Key 时对话流正常建立，首个 error 事件说明原因，不影响其余接口。
