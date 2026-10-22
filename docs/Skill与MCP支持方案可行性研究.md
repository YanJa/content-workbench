# Skill 与 MCP 支持方案可行性研究

> 结论先行：**两者都可行，但性质完全不同**。Skill 是低风险的「给模型补流程知识」，1～2 天可落地且几乎不改架构；MCP 是「接入外部工具生态」，安全面与运维面都大得多，建议分阶段、默认关闭、白名单制。
>
> 本文所有结论均经**实测验证**（在沙箱内真实安装 `mcp 2.3.0` + `skills-ref 0.1.1`，跑通全量 167 项测试），非纸面推演。

---

## 一、先弄清两者是什么

社区常把 MCP 和 Skill 混为一谈，但它们的**职责正交**：

| | MCP（Model Context Protocol） | Agent Skills |
|---|---|---|
| 本质 | **连接协议** —— 把外部系统的能力暴露成工具 | **文件约定** —— 把「怎么做某类任务」写成 Markdown |
| 解决什么 | 模型够不着的**外部能力**（查数据库、调 SaaS API、读文件） | 模型不知道的**流程知识**（你们公司的排版规范、审稿 checklist） |
| 谁控制 | model-controlled（模型自己决定调） | 模型按需激活，也支持用户显式 `/skill-name` 调用 |
| 交付形态 | 网络/进程端点（stdio 或 HTTP） | 一个目录 + `SKILL.md` |
| 对应到本平台 | 扩展现有 **ToolRegistry** | 扩展**系统提示词 + 知识库** |

官方口径（[Claude Help Center](https://support.claude.com/articles/12512176)）明确说两者**互补**：

> "MCP connects Claude to external services and data sources. Skills provide procedural knowledge — instructions for how to complete specific tasks or workflows. **You can use both together**: MCP connections give Claude access to tools, while Skills teach Claude how to use those tools effectively."

**对本平台的判断**：你们的 `ToolRegistry` 已经是标准 function calling，MCP 是「把手写工具换成协议接入」；而 Skill 的价值更大 —— 现在的 `prompts/platforms.py` 和 `prompts/layout_prompt.py` 是硬编码的，Skill 能把「平台调性、排版主题偏好、行业话术」变成用户可自助维护的资产。

---

## 二、Skill 可行性：高，且收益直接

### 2.1 规范现状（已确认为开放标准）

Skill 早已**不是 Anthropic 私有**。官网 [agentskills.io](https://agentskills.io)，仓库 [agentskills/agentskills](https://github.com/agentskills/agentskills)（代码 Apache-2.0），并有 40+ 厂商支持：OpenAI ChatGPT/Codex、Google Gemini CLI、Microsoft VS Code/GitHub Copilot、Cursor、JetBrains、Mistral、Spring AI 等（[客户列表](https://agentskills.io/clients)）。

### 2.2 文件格式（实现依据）

```
my-skill/
├── SKILL.md          # 必需
├── references/       # 可选：参考文档，按需读取
├── scripts/          # 可选：脚本
└── assets/           # 可选：模板、图片
```

`SKILL.md` 的 YAML frontmatter：

| 字段 | 必填 | 约束 |
|---|---|---|
| `name` | ✅ | 1–64 字符，仅 `a-z0-9-`，首尾不能是连字符，不能有 `--`，**必须等于父目录名** |
| `description` | ✅ | 1–1024 字符，**要同时写清「做什么」和「何时用」**（模型靠这句决定激活） |
| `license` / `compatibility` / `metadata` / `allowed-tools` | ❌ | `allowed-tools` 为实验性 |

（来源：[specification](https://agentskills.io/specification)）

### 2.3 核心机制：渐进式披露（progressive disclosure）

这是 Skill 省 token 的关键，分三层：

1. **Catalog**：启动时只把 `name + description` 放进上下文，每个 skill 约 **50–100 tokens**
2. **Instructions**：模型判断需要时，再读 `SKILL.md` 全文（建议 <5000 tokens / <500 行）
3. **Resources**：`references/`、`scripts/` 只在真的用到时才读

**为什么这很重要**：20 个 skill 的 catalog 也才 ~2000 tokens，比塞 20 份完整规范省两个数量级。

### 2.4 在「非 Claude 模型」上怎么落地（本平台的关键）

我们用的是 DeepSeek / Cloudflare，不是 Claude。官方指南（[adding-skills-support](https://agentskills.io/client-implementation/adding-skills-support.md)）给了两条路径：

| 路径 | 做法 | 本平台适用性 |
|---|---|---|
| **A. 专用工具** | 注册 `activate_skill(name)` 工具，`name` 用 **enum 约束**到合法 skill 集合，返回正文 | ✅ **推荐**，直接复用现有 ToolRegistry |
| B. 文件读取 | 模型用 file-read 工具读 `SKILL.md` | ❌ 我们是纯 API 调用，模型没文件系统 |

官方还强调几条硬要求，正好和现有架构对得上：

- catalog 放在**工具描述或 system prompt** 里
- 用户**显式激活**（`/skill-name`）要支持
- **没有可用 skill 时不要注册空工具**
- 被权限过滤掉的 skill 要**从 catalog 里彻底隐藏**，而不是激活时才拒绝

### 2.5 我们的落地方案

```
实现要点（约 400 行）
├─ SkillRegistry：扫描 / 上传 → 解析 frontmatter → 校验 name 规范
├─ 存储：DB 表 kb_skills(owner, name, description, body, enabled)
│        原始文件走 UPLOAD_DIR/skills/<owner>/<name>/
├─ 注入：system prompt 追加 catalog（name + description）
├─ 工具：registry 动态注册 activate_skill(name: enum)
└─ 前端：设置页管理（列表 / 启用禁用 / 上传 zip / 在线编辑 SKILL.md）
```

**产物复用**：Skill 本质上就是「结构化知识 + 触发条件」。它和刚做完的知识库高度相似，但**不要合并** —— 知识库是语义召回（碎片、被动注入），Skill 是显式激活（完整、主动加载），混在一起会让两者都变差。

**风险与成本**

| 项 | 评估 |
|---|---|
| 技术风险 | **低**。纯 Python 字符串处理，无新依赖（`skills-ref` 可选，仅用于校验） |
| 对现有功能影响 | **无**。工具是新增的，catalog 是追加在 prompt 尾部 |
| 工作量 | **1–2 天**（含前端管理页） |
| 主要坑 | ① `name` 必须等于目录名，上传时容易不匹配 ② description 写太烂 → 模型该激活时不激活 ③ catalog 太长挤压正文预算 |

**结论：强烈建议做，且应该先做。** 它给的是「用户自助沉淀方法论」的能力，和平台定位（内容运营）天然契合。

---

## 三、MCP 可行性：可行，但要按「高风险集成」对待

### 3.1 规范版本是个大坑（必须先讲清）

截至 2026-10，MCP 最新规范是 **2026-07-28**，相比 2025-11-25 是**破坏性重构**：

| 变化 | 影响 |
|---|---|
| **协议改为无状态**：删除 `initialize` 握手和 `Mcp-Session-Id` | 连接管理简化，但老 server 不兼容 |
| 版本/能力改由每个请求的 `_meta` 携带 | 需 SDK 自动协商 |
| 传输只剩 **stdio** 和 **Streamable HTTP** | **旧 HTTP+SSE 已正式废弃** |
| 新增 **MRTR**（多轮往返）：`resultType:"input_required"` | 适合做「危险操作二次确认」 |
| 订阅改为 `subscriptions/listen` 长流；`ping`/`logging/setLevel` 删除 | — |

（来源：[spec](https://modelcontextprotocol.io/specification/latest)、[changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)）

⚠️ **现实风险**：现网大量 MCP server 还是 2025 版（带握手）。好消息是 SDK 的 `mode='auto'` 会自动探测降级 —— 这也是**绝对不要手写 JSON-RPC** 的理由，否则一次规范升级就是重写。

### 3.2 依赖冲突：实测通过 ✅

这是我最担心的点，已实际验证：

| 项 | `requirements.txt` 锁定 | 实际安装 | mcp 2.3.0 要求 | 结论 |
|---|---|---|---|---|
| pydantic | 2.10.4 | **2.12.5** | `>=2.12.0` | ✅ 实际满足（锁文件已过期，需同步） |
| fastapi | 0.115.6 | **0.128.2** | — | ✅ |
| starlette | 间接 | **0.50.0** | `>=0.27`（3.14 才要 0.48+） | ✅ |
| Python | — | 3.11.1 | `>=3.10` | ✅ |

**实测结果**：`pip install mcp` 成功，`from mcp import Client` 正常，`import app.main` 无异常，**全量 167 项测试全部通过**。

> 注意：`requirements.txt` 里写的 2.10.4 与实际运行的 2.12.5 不符，说明锁文件长期没更新。**接入 MCP 前应先把这个锁文件对齐**，否则在干净环境部署会踩到真实的版本冲突。

### 3.3 SDK 能力已确认可用

```python
from mcp import Client

# 高层封装，一个对象管完生命周期，mode='auto' 自动双版本协商
async with Client(url, mode="auto", read_timeout_seconds=30) as c:
    print(c.server_info, c.protocol_version, c.instructions)
    tools = await c.list_tools()
    result = await c.call_tool("search", {"q": "..."})
```

实测 `Client` 公开方法完整：`list_tools / call_tool / list_resources / read_resource / list_prompts / get_prompt / subscribe_resource / listen`，且支持 `elicitation_callback`（对接现有的二次确认流程）。

**一个必须知道的语义坑**：工具执行失败**不抛异常**，而是返回 `is_error=True`；只有 JSON-RPC 层错误才抛 `MCPError`。两者要分开处理 —— 前者回喂模型自纠，后者才重试/降级。

### 3.4 安全模型：这是真正的拦路虎

MCP 规范对此有 MUST 级要求，**不是可选建议**：

| 风险 | 规范要求 | 我们的应对 |
|---|---|---|
| **工具描述投毒** | 工具注解/描述「除非来自可信 server，一律视为不可信」 | 接入需管理员审批；描述展示给用户看 |
| **Rug pull**（接入后偷偷改工具定义） | 客户端 SHOULD 校验工具结果、审计 | **对工具定义做 hash 固定**，变更时强制二次确认 |
| **Cross-server shadowing** | — | 不同信任等级的 server **不要同会话共存** |
| **Token 透传** | server **MUST NOT** 接受非自己签发的 token | 严禁把用户 JWT 转发给下游 server |
| **混淆代理** | `redirect_uri` 精确匹配、state 一次性 | OAuth 接入时严格遵守 |
| **SSRF** | 有专门章节 | 只允许 HTTPS + 域名白名单，禁内网地址 |

来源：[security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)、[Tools#Security](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

第三方研究显示工具投毒攻击在基准测试中**平均成功率 36.5%**（[MCPTox 综述](https://www.mayrian.com/insights/tool-poisoning-how-attackers-hijack-ai-agents-through-mcp)），且攻击常靠 UI 隐藏参数实现 —— 我们现有的确认弹窗**必须展示完整参数**，这点现有实现已经做到了。

### 3.5 与现有架构的整合点

好消息是**接口很干净**，`ToolRegistry` 本身就是天然的适配层：

```python
# 现有：装饰器注册本地工具
@tool(name="query_queue", ...)
async def query_queue(...): ...

# 新增：把 MCP server 的工具动态注册进同一个 registry
for t in await mcp_client.list_tools():
    registry.register_external(
        name=f"{server_ns}__{t.name}",      # 必须加命名空间
        description=t.description,
        parameters=t.inputSchema,
        handler=make_proxy(mcp_client, t.name),
        requires_confirm=需审批,
    )
```

**必须处理的设计点**：

| 问题 | 方案 |
|---|---|
| **工具名冲突** | 规范明确唯一性**只限单个 server 内**，客户端 SHOULD 用 server id 前缀消歧 → 统一 `serverNs__toolName` |
| **工具数量爆炸** | 全量注入会撑爆上下文 → 学 n8n 的「Tools to Include」**勾选子集** + 默认只注入 top-N |
| **连接管理** | 远程 HTTP 用连接池 + `ttlMs` 缓存 `tools/list`；**stdio 本地进程必须长驻**，每次新建代价极高 |
| **多租户凭证** | 每个用户配自己的 server 凭证，加密存库（复用现有 security 模块），**不跨租户共享** |
| **超时** | 官方建议首响应按 ~30s 设计；现有 `registry.execute` 的 300s 超时对 MCP 太宽松，需单独设 |
| **危险操作** | MCP 工具默认 `requires_confirm=True`（**fail-closed**），管理员可逐个放行 |

### 3.6 关键判断：要不要支持 stdio？

**建议 MVP 不支持 stdio，只支持 Streamable HTTP。**

理由：

1. stdio 意味着**在服务器上执行任意本地进程** —— 这等于给用户开了 shell，风险等级完全不是一个量级
2. 长驻进程管理、崩溃重启、资源隔离，运维成本高
3. 从「内容运营平台」的定位看，用户需要的多半是远程 SaaS 接口（搜索、图床、数据分析），而不是本地进程

如果将来真要支持，必须：容器隔离 + 资源限额 + 只允许管理员配置 + 白名单二进制。

### 3.7 风险与成本汇总

| 项 | 评估 |
|---|---|
| 技术风险 | **中高**。协议版本断层 + 外部 server 质量参差 |
| 安全风险 | **高**。本质是「让第三方代码影响你的 LLM 行为」 |
| 工作量 | **5–8 天**（MVP：HTTP-only + 白名单 + 工具子集 + 确认） |
| 最大未知 | 现网 server 对 2026 规范的支持率 —— **必须先做连通性探测** |

---

## 四、推荐路线：Skill 先行，MCP 小步试水

### 阶段 0：地基（0.5 天，**做 MCP 之前必须先做**）

- 把 `requirements.txt` 与实际版本对齐（pydantic 2.12 / fastapi 0.128）
- `ToolRegistry` 抽成 `Tool` 抽象：本地工具 / MCP 代理工具统一接口
- 工具名加命名空间支持

### 阶段 1：Skill（1–2 天）⭐ 优先 —— **✅ 已完成（2026-10-22）**

- ✅ Skill 存储 + 解析 + 校验（`name` 必须等于文件名，等价于官方的「等于父目录名」）
- ✅ `activate_skill` 动态工具 + catalog 注入（独立 system 消息）
- ✅ 前端管理页（上传 SKILL.md / 粘贴正文 / 启停 / 改名 / 查看正文）
- ✅ 验收通过：上传「公众号爆款标题」skill 后，对话说「帮我起几个公众号标题」，
  模型自动调 `activate_skill({"name":"wechat-hot-title"})`，并按正文规则输出
  （5 个候选 + 每条一句点击理由、数字开头、不复述技能存在）

**实际与计划的偏差（都是实测后改的）：**

| 计划 | 实际 | 原因 |
|---|---|---|
| 上传 zip | 只收单个 `.md` | 阶段一只做正文注入，`references/` 用不上；收下 zip 却用不上其中文件是误导 |
| 静态 `@tool` 注册 | 新增 `dynamic_tool` + `schemas_for(ctx)` | 注册表是导入期单例，装不下「enum 随用户变化」 |
| 解析失败覆盖同名旧版本 | 同名已有可用版本时**拒绝覆盖**（400） | 实测踩坑：误传坏文件会把能用的技能打成 invalid，无法撤销 |

剩余（阶段 1.5，未做）：`references/*.md` 的按需读取（L3 渐进式披露）、zip 打包上传、
技能导入导出（方便在用户之间传）。

实施细节见 `docs/架构设计.md` §6.5 与 `docs/API接口文档.md` 的「技能包（Skill）」章节。

### 阶段 2：MCP MVP（5–8 天）

- 仅 Streamable HTTP + 仅管理员配置（白名单）
- 工具子集勾选 + `serverNs__tool` 命名 + 工具定义 hash 固定
- 默认 `requires_confirm=True`
- 连通性探测接口（用户可先「测试连接」看到工具列表再决定启用）
- **验收**：接入 1 个公开 server（如 Fetch），对话中调用成功，确认弹窗展示完整参数

### 阶段 3（可选）：双向能力

- **把平台暴露成 MCP server**：让 Claude/Cursor 直接调用「查队列 / 存素材 / 推草稿」。这个方向价值被低估 —— 用户可能更想「在自己的编辑器里操作工作台」，而不是「在工作台里接别人的工具」
- 注意：MCP 已有 **Skills over MCP** 官方扩展（[SEP-2640](https://modelcontextprotocol.io/seps/2640)，已 Final），Skill 可以通过 `skill://` URI 走 MCP 通道分发，阶段 1 的实现要为这个留口子

---

## 五、结论

| 问题 | 回答 |
|---|---|
| Skill 可行吗？ | **可行，风险低，收益高。建议立即做**，1–2 天 |
| MCP 可行吗？ | **可行，实测依赖无冲突**，但安全面大，需 5–8 天 + 严格白名单 |
| 最大风险是什么？ | 不是技术，是**信任边界** —— MCP 让第三方影响你的模型行为 |
| 最该先做什么？ | ① 对齐 `requirements.txt` ② 做 Skill ③ MCP 只做 HTTP + 管理员白名单 |
| 有没有被低估的方向？ | **把平台本身暴露成 MCP server**，让用户的 AI 客户端直接操作工作台 |

---

## 附：本文的实测记录

调研期间在沙箱内**临时安装**了 `mcp 2.3.0` 验证兼容性，验证完成后已**全部卸载回退**，未改动运行环境。

| 验证项 | 结果 |
|---|---|
| `pip install mcp`（2.3.0） | ✅ 成功，无依赖冲突 |
| `from mcp import Client` | ✅ 正常，`mode='auto'` 默认值确认 |
| `import app.main` | ✅ 无异常 |
| 全量后端测试 | ✅ **167 passed** |
| 卸载后关键测试 | ✅ **80 passed**，应用正常导入 |
| `skills-ref 0.1.1` 在 PyPI | ✅ 可下载 |

**mcp 会连带引入的包**（接入时需写进锁文件）：

| 包 | 变化 | 说明 |
|---|---|---|
| `mcp` | 新增 2.3.0 | 主包 |
| `mcp-types` | 新增 2.3.0 | 2.x 起拆出的类型包 |
| `sse-starlette` | 新增 3.5.0 | — |
| `pydantic-settings` | **2.7.0 → 2.15.0** | ⚠️ 会被升级，需回归验证配置加载 |
| `pydantic` / `fastapi` / `starlette` | 满足，无需变动 | 2.12.5 / 0.128.2 / 0.50.0 |

### ⚠️ 锁文件与实际情况不符（接入前必须先修）

| 包 | `requirements.txt` 写的 | 实际运行 |
|---|---|---|
| pydantic | 2.10.4 | **2.12.5** |
| fastapi | 0.115.6 | **0.128.2** |
| starlette | 未锁 | 0.50.0 |

锁文件已明显落后于实际环境。这意味着**在干净机器上重新 `pip install -r requirements.txt`，装出来的是一套没被验证过的组合**。接入 MCP 前应先把锁文件对齐到实测通过的版本，否则会遇到「本地正常、新部署报错」的经典问题。
