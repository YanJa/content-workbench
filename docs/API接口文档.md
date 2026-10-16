# API 接口文档

Base URL：`/api`（前端 axios baseURL 同此）。交互式文档：后端启动后访问 `http://localhost:8000/docs`（Swagger）或 `/redoc`。

所有时间戳均为 Unix 秒。除对话流式接口外，请求/响应均为 JSON。

---

## 数据归属（owner）

用户模块上线前的隔离口径。所有业务接口（队列/内容/排版/视频/公众号/对话/用量/音色）都按 owner 过滤数据。

- 普通请求带请求头 `X-Owner-Id: <owner>`（前端 axios 拦截器自动带上，无需手动）；
- `<img>` / `<audio>` / `<video>` 这类浏览器直链没法带请求头，用查询参数 `?owner=<owner>`（配图、成片、试听等静态服务 URL 由服务端拼好）；
- **都不传 = `default`**：兼容 curl、老前端与存量数据；
- 格式限 `[A-Za-z0-9._-]{1,64}`（owner 会进文件路径，防穿越），不合法返回 **400**；
- 访问别人的数据与「数据不存在」同响应（404），不泄露存在性；
- 用户模块上线后改为从登录态解析 owner，客户端传的值不再被信任（接口契约不变）。

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
  "tools_enabled": true
}
```

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
> **1.7~2.9 秒**（跨境 + 反向隧道 + basic auth），每 2 秒传一次 22KB 纯浪费。
>
> `/jobs/{id}/progress` 只回进度必需字段（`id/status/progress/stage/error/
> output_url/output_path/duration_sec/size_bytes/finished_at`），实测
> **267 字节**，缩减 98.8%。
>
> 前端配合两点：**自适应间隔**（有变化 4s，无变化退避到 8s→15s）+
> **结束时取一次完整详情**回填 script / visual_scenes。

### 声音复刻

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

## 错误约定

- 业务错误：`{"detail": "中文说明"}` + 合适的 4xx（404 不存在 / 422 参数问题）；
- 非法的 owner 标识返回 **400**（格式校验失败，见上文「数据归属」）；
- 工具内部失败不抛 HTTP 错误，而是 `result.error` 带回，前端按失败标记渲染；
- AI 未配置 Key 时对话流正常建立，首个 error 事件说明原因，不影响其余接口。
