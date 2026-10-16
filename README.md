# 内容运营工作台（Content Workbench Pro）

> Vue 3 + TypeScript + Naive UI 前端 · FastAPI + 异步 SQLAlchemy 后端 · DeepSeek 驱动 · Function Calling 工具链

从 [demo 初稿](../content-workbench/index.html) 演进而来的正式工程化版本：AI 多轮对话创作三平台内容（公众号 / 小红书 / 抖音）、发布队列管理、公众号一键推草稿箱、视频异步渲染管线。

![桌面端](docs/screenshots/desktop_chat.png)

## 核心能力

| 能力 | 状态 | 说明 |
|---|---|---|
| AI 多轮对话创作 | ✅ 可用 | SSE 流式输出，会话持久化到数据库，超长自动摘要压缩 |
| 三平台文案生成 | ✅ 可用 | 公众号 / 小红书 / 抖音各有独立系统提示词与合规检查 |
| Function Calling | ✅ 可用 | 5 个内置工具，危险操作（推送/渲染）必须人工确认 |
| 公众号推草稿箱 | ✅ 可用 | 官方 `draft/add` 接口（个人订阅号可用），需配 IP 白名单 |
| 发布队列 | ✅ 可用 | 状态流转 + 分平台发布检查清单 + Markdown 导出 |
| 视频异步渲染 | ⚙️ 骨架 | 任务状态机与轮询接口已通，渲染步骤待接 ffmpeg/edge-tts |
| PPT 转视频 | 🅙 规划 | 解析管线已预留接口位 |

## 快速开始

### 1. 后端（uv）

```bash
cd backend
uv venv --python 3.11
uv pip install -r requirements.txt          # 国内加速见下方"国内源"

cp .env.example .env
# 编辑 .env，至少填 DEEPSEEK_API_KEY

.venv/bin/python -m uvicorn app.main:app --reload --port 8000
# 接口文档：http://localhost:8000/docs
```

没用 uv 的话，`python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` 等价可用。

没配 API Key 也能启动，AI 对话会返回 error 事件，其余功能（队列/工具/健康检查）正常。

### 2. 前端

```bash
cd frontend
pnpm install
pnpm dev
# 打开 http://localhost:5173，Vite 已把 /api 代理到 8000 端口
```

### 国内源（强烈建议）

`uv pip install` 默认走 PyPI，国内会很慢。配一次永久生效：

```bash
mkdir -p ~/.config/uv
cat > ~/.config/uv/uv.toml <<'EOF'
[[index]]
url = "https://pypi.tuna.tsinghua.edu.cn/simple"
default = true
EOF
```

也可以临时指定：`uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt`

npm 换源：`npm config set registry https://mirrors.tencent.com/npm/`

实测：全部后端依赖在清华源下 **约 5 秒**装完。

### 3. 上服务器

不走容器，用 **uv + systemd + nginx**，完整步骤见 [deploy/README.md](deploy/README.md)。

## 目录结构

```
content-workbench-pro/
├── backend/
│   ├── app/
│   │   ├── core/            # 配置、缓存（Redis/内存双实现）、日志
│   │   ├── db/              # 异步 SQLAlchemy 会话
│   │   ├── models/          # Conversation / Message / Content / QueueItem
│   │   ├── schemas/         # Pydantic 请求/响应模型
│   │   ├── routers/         # chat(SSE) / queue / tools / video
│   │   ├── services/        # llm(模型路由) / agent(编排) / conversation / wechat / render
│   │   ├── prompts/         # 三平台系统提示词
│   │   └── tools/           # 工具注册中心 + 内置工具
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/             # axios 封装 + SSE 手动解析
│       ├── stores/          # Pinia：chat / queue
│       ├── views/           # Chat / Queue / Video / Settings 四页
│       └── types/           # 与后端 schema 一一对应的 TS 类型
├── deploy/                  # nginx 配置、systemd 单元、部署手册
└── docs/                    # 架构、API、开发指南、交付说明、截图
```

## 技术决策速记

详细论证见 [docs/架构设计.md](docs/架构设计.md)，要点：

- **不上 LangChain 等重型 Agent 框架**。内容创作是确定性工作流，手写编排（`services/agent.py`，最多 5 轮工具循环）+ DeepSeek 原生 Function Calling 足够，少一层抽象少一类 bug。
- **前端 Vue 3 + TS + Naive UI**（团队指定的选型）。SSE 用 `fetch + ReadableStream` 手动解析——`EventSource` 只支持 GET，而对话是 POST。
- **缓存双实现**：`REDIS_URL` 留空自动降级内存缓存，本地开发零依赖，生产多进程部署建议配 Redis。
- **部署不用容器**：uv 管依赖、systemd 守护进程、nginx 托管前端。依赖装得飞快，排查问题所见即所得。
- **危险工具必须人工确认**。推送草稿箱、提交渲染带 `requires_confirm` 标记，模型只产生调用意图，执行权在用户手里——这既是《微信公众平台运营规范》的合规要求，也是账号安全底线。

## 关键合规边界

> ⚠️ 《微信公众平台运营规范》禁止「利用 AI、脚本、接口或其他自动化方式替代真人完成内容创作、发布」。

本工作台的定位是**辅助创作**：AI 生成 → 人工审核修改 → 人工触发推送草稿箱 → 人工在公众号后台群发。`freepublish` 自动群发接口对个人订阅号已回收，也不要尝试模拟浏览器自动群发——已有封号案例。

## 文档索引

- [架构设计](docs/架构设计.md) — 分层结构、Agent 编排、上下文管理、成本优化
- [API 接口文档](docs/API接口文档.md) — 全部 24 个路由 + SSE 事件协议
- [开发指南](docs/开发指南.md) — 加新工具/新平台/新视图的步骤、常见坑
- [部署手册](deploy/README.md) — uv 装依赖、systemd 守护、nginx 反代、切 Postgres、IP 白名单
