# AI 用量模块 设计方案

> 目标：能回答四个问题 —— **花了多少 tokens、花了多少钱、花在哪、谁花的**。
> 本文基于对现有代码的实测盘点，不是凭空设计。

---

## 一、现状盘点（先说清楚现在缺什么）

### 1.1 已经有的

| 资产 | 位置 | 说明 |
|---|---|---|
| 单条消息 token 数 | `messages.prompt_tokens` / `completion_tokens` | 只记在 assistant 消息上 |
| 思维链 token | `llm.Usage.reasoning_tokens` | **算出来了，但没落库** |
| 高峰时段判断 | `services/llm.py: is_peak_hour()` | 高峰价格翻倍的逻辑前提已有 |
| 模型路由 | `MODEL_ROUTES`（copywriting/reasoning/lightweight/vision） | 不同任务用不同模型，价格不同 |
| 前端单条展示 | `ChatView.vue` usage 行 | 已有 `入/出/思维链` 显示 |

### 1.2 缺的（这才是要建模块的原因）

| 缺口 | 后果 |
|---|---|
| **没有模型名字段** | 无法按模型计价，flash 和 pro 价格差好几倍 |
| **没有 reasoning_tokens 落库** | 思维链占比看不出来（实测思维链能占 50%+） |
| **没有缓存命中字段** | DeepSeek 缓存命中价是未命中的 **1/50**，这是费用大头，现在是盲区 |
| **没有耗时字段** | 没法看"慢请求"和异常调用 |
| **没有独立用量表** | 一次对话可能触发多次 LLM 调用（工具循环 + 摘要），一行 message 存不下 |

### 1.3 三个正在丢数据的 bug（必须顺手修）

| # | 位置 | 问题 |
|---|---|---|
| 1 | `services/conversation.py: maybe_summarize()` | 压缩历史时调用 LLM，**这次调用完全没记账**。长对话越用越贵，而这部分成本现在是不可见的 |
| 2 | `routers/chat.py` 落库处 | `if collected_text:` 才落库 —— 纯工具调用回合（没正文）的用量**直接丢掉** |
| 3 | `routers/chat.py` 工具确认分支 | 先落一条 assistant 消息，`prompt_tokens=0`，后续真实用量记在另一条上，统计偏低 |

---

## 二、设计总览

```
采集            存储              计价              API               展示
─────────────────────────────────────────────────────────────────────
LLMService  ──► llm_usage 表 ──► pricing.py ──► /api/usage/* ──► UsageView.vue
(统一埋点)      (独立表)        (单价+峰谷)      summary/records    + ChatView 联动
```

核心思路一句话：**在 LLM 调用层做唯一埋点**，而不是让每个业务方记得记账——后者一定会漏（上面三个 bug 就是证据）。

---

## 三、采集层：在 LLM 层做唯一埋点

### 3.1 给 `LLMService` 加一个用量回调

```python
# services/llm.py
UsageSink = Callable[[UsageRecord], Awaitable[None]]

class LLMService:
    async def chat(self, messages, *, task="copywriting", usage_sink: UsageSink | None = None, **kw):
        ...
        if usage_sink:
            await usage_sink(UsageRecord(model=route.model, task=task, usage=usage, latency_ms=...))
```

为什么不放业务层：业务层每次都要记得传/累加，一漏就是永久缺失；放 LLM 层则**只要调用了模型就一定有记录**，包括以后新增的调用点。

`chat()` 和 `chat_stream()` 两个出口都要接。

### 3.2 补两个字段的解析（现在没取）

```python
# 现在只取了 completion_tokens_details.reasoning_tokens
# 需要补 prompt 侧的缓存信息（DeepSeek 返回 prompt_tokens_details.cached_tokens）
prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
cached = getattr(prompt_details, "cached_tokens", 0) or 0
# cache_miss = prompt_tokens - cached
```

### 3.3 埋点位置清单

| 调用点 | 是否记账 | 说明 |
|---|---|---|
| `routers/chat.py` 主对话流 | ✅ 通过 sink 自动记 | 含工具循环里的多轮调用，每轮一条 |
| `conversation.maybe_summarize()` | ✅ 补上（现在丢） | 标 `is_summary=true`，可单独看"压缩开销" |
| 未来新增调用点 | ✅ 自动覆盖 | 只要走 `LLMService` |

---

## 四、存储层：新增 `llm_usage` 表（不动 `messages`）

### 4.1 为什么不扩 `messages` 表

一次用户提问可能对应 **N 次** LLM 调用（工具来回 + 摘要 + 不同模型），`messages` 一行存不下；而且 ALTER TABLE 在现有 SQLite 上没有迁移工具（项目未引入 Alembic），改老表风险高。**新表是零风险的加法**。

### 4.2 表结构

```python
class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str]                    # uuid
    conversation_id: Mapped[str | None]
    message_id:      Mapped[str | None]  # 摘要类调用为 None
    owner:  Mapped[str]                 # 沿用 messages 的 owner 约定
    scene:  Mapped[str]                 # gzh / xiaohongshu / douyin / chat

    model:     Mapped[str]              # deepseek-flash / deepseek-v4-pro
    task_type: Mapped[str]              # copywriting / reasoning / lightweight / vision

    prompt_tokens:     Mapped[int]
    completion_tokens: Mapped[int]
    reasoning_tokens:  Mapped[int]      # 思维链（含在 completion 内，不重复计价）
    cached_tokens:     Mapped[int]      # 命中缓存部分 ← 便宜 50 倍，必须单独记
    total_tokens:      Mapped[int]

    latency_ms:  Mapped[int]
    is_peak:     Mapped[bool]           # 复用 is_peak_hour()
    is_summary:  Mapped[bool]           # 是否为历史压缩调用
    created_at:  Mapped[int]            # unix 秒

    __table_args__ = (
        Index("ix_usage_created", "created_at"),
        Index("ix_usage_model_created", "model", "created_at"),
        Index("ix_usage_conv", "conversation_id"),
    )
```

### 4.3 迁移与历史数据

- `db/session.py` 的 `init_db()` 走 `create_all`，**新表自动建**；必须在 `models/__init__.py` 里导出，否则 `create_all` 看不到（这个坑 `session.py` 注释里已经写了）。
- 历史数据：现有 `messages` 里已有 token 数，提供一个 **一次性回填脚本** `scripts/backfill_usage.py`，把已有 assistant 消息的用量导进新表，`model` 填 `unknown`（当时没记，不能编）。回填与否由你定，见第八节。

---

## 五、计价层：`core/pricing.py`

### 5.1 单价表（集中放、可覆盖，兼容其它模型）

单价是**数据**不是逻辑，三层覆盖（后者覆盖前者）：

1. **内置默认值**：`core/pricing.py` 里的 `PRICING`（DeepSeek 官方价）
2. **外部 JSON 文件**：`backend/config/pricing.json`（或 `PRICING_FILE` 指路径）——**加一个新模型只需要在这里加一条，不改代码**
3. **环境变量**：`PRICING_JSON`（临时调试 / 容器注入）

一条单价支持的字段（除 `in`/`out` 外全可选）：

```jsonc
{
  "qwen-max": {
    "label": "通义千问 Max",       // 展示名，前端直接显示
    "vendor": "aliyun",             // 厂商，用于分组
    "in": 20.0,                      // 输入（未命中缓存）
    "out": 60.0,                     // 输出
    "in_cached": 2.0,                // 输入（缓存命中）→ 不填则按 in 计
    "context_window": 262144,        // 仅展示
    "reasoning": false,              // 是否推理模型（思维链计入输出）
    "peak_multiplier": 1.0,          // 高峰倍数 → 1.0 表示该模型无峰谷
    "tiers": [                       // 长上下文档位价（整档价，不是分段累加）
      {"above": 128000, "in": 40.0, "out": 120.0}
    ]
  }
}
```

- 改完文件调 `POST /api/usage/pricing/reload` **热加载，不用重启**；插件/代码里也能 `register_model(name, rules)`。
- **未登记模型**：按 `_default`（Flash）价兜底，同时 `known=false` 透到前端，UI 显式提示「按兜底价估算」，绝不默默算错还装作没事。
- 现成的参照：`backend/config/pricing.example.json`（含阿里云/智谱/Anthropic/OpenAI 的格式样例，价格为占位值，按厂商最新价校准后再改名为 `pricing.json` 启用）。

### 5.2 计算口径（三条容易算错的地方）

1. **思维链不额外计价**：`reasoning_tokens` 含在 `completion_tokens` 里，按 out 单价算一次即可，**不要 in+out+reasoning 三者相加**。
2. **缓存部分单独算**：`cached_tokens` 按 `cached` 单价，`prompt_tokens - cached_tokens` 按 `in` 单价。
3. **高峰乘 2**：`is_peak=true` 的记录整体 ×2。

### 5.3 定性

UI 上必须写 **"估算值，以服务商账单为准"**。原因：单价来自配置、缓存命中率依赖服务端返回、峰谷规则依赖本地时间判断。它是**成本感知工具，不是对账工具**。

---

## 六、API 层：`routers/usage.py`

| 接口 | 参数 | 返回 |
|---|---|---|
| `GET /api/usage/summary` | `days`、`group_by=day\|model\|scene` | 总 tokens / 总费用 / 调用次数 / 平均耗时 + 分组序列 |
| `GET /api/usage/records` | `limit`、`offset`、`model`、`scene`、`conv_id`、`start`、`end` | 明细分页 |
| `GET /api/usage/top-conversations` | `days`、`limit` | 消耗 Top N 会话（带标题，能直接点进去看） |
| `GET /api/usage/pricing` | — | 当前单价表（`items`）+ 加载来源（`state`：文件/环境变量有没有生效） |
| `POST /api/usage/pricing/reload` | — | 改完 `config/pricing.json` 后热加载，不用重启 |

实现要点：

- **聚合在 Python 侧做**，不用 `strftime('%Y-%m-%d', created_at,'unixepoch')` 这类方言 SQL —— 数据量小（日均几百条），Python 聚合可读性好，将来换 PostgreSQL 也不用改。
- `main.py` 里 `include_router(usage.router, prefix=settings.API_PREFIX)`。

---

## 七、展示层：新增 `UsageView.vue`

### 7.1 页面结构

```
┌─────────────────────────────────────────────────┐
│ KPI 卡：今日 tokens │ 今日费用 │ 本月费用 │ 均耗时 │
├─────────────────────────────────────────────────┤
│ [今日] [7天] [30天] [全部]          估算值，以账单为准 │
├─────────────────────────────────────────────────┤
│ 趋势图：按天堆叠柱（输入 / 输出 / 缓存命中）        │
├──────────────────────┬──────────────────────────┤
│ 按模型分布（横条）     │ 按场景分布（横条）          │
├─────────────────────────────────────────────────┤
│ 明细表：时间 会话 模型 入 出 思维链 缓存 耗时 费用  │
├─────────────────────────────────────────────────┤
│ 消耗 Top 会话榜                                  │
└─────────────────────────────────────────────────┘
```

### 7.2 图表选型（三选一，需要你拍板）

| 方案 | 代价 | 适用 |
|---|---|---|
| **A. 自绘 SVG** | 约 150 行，**零新依赖**，包体不变 | 只要柱状趋势 + 横条占比，A 完全够 |
| B. 引入 ECharts | +约 150KB(gzip)，功能全 | 后面要做多维度交互、缩放、导出图 |
| C. chart.js / 轻量库 | +30~60KB | 折中 |

**我推荐 A**：这个页面的图形就是"按天堆叠柱 + 横条占比"，自绘反而更好控样式（暗色主题统一），也不给 757KB 的 naive-ui 包再添负担。

### 7.3 顺带的低成本高价值改动

- `ChatView.vue` 输入框上方加一行：**"本会话已用 X tokens ≈ ¥Y"**（实时感最强）
- 单条消息的 usage 行补上**模型名**和**费用**，让"贵在哪"当场可见

### 7.4 接入点

- `router/index.ts` 加 `/usage` 路由
- `App.vue` 的 `NAV` 数组加一项（单一数据源，桌面侧栏和移动底栏自动同步）
- `components/NavIcon.vue` 加一个 `usage` 图标（线性风格，和现有四个一致）

---

## 八、分期与工作量

| 阶段 | 内容 | 预估 |
|---|---|---|
| **P0** | `llm_usage` 表 + LLM 层埋点（含摘要修复）+ `pricing.py` + summary/records API + 用量页 KPI/趋势/明细 | 后端约 300 行，前端约 350 行 |
| **P1** | Top 会话榜、按模型/场景分布、ChatView 会话级计数 + 消息级费用 | 约 120 行 |
| **P2** | 预算告警（日/月上限，超限提示）、CSV 导出、按 owner 分摊 | 另议 |

---

## 九、风险与取舍

| 风险 | 处理 |
|---|---|
| 费用是估算不是账单 | UI 明确标注；不提供"对账"语义 |
| 单价会过时/模型会改名 | 常量集中 + `PRICING_JSON` env 覆盖 |
| 历史数据没有模型名 | 回填时标 `unknown`，统计时单独归一类，不假装知道 |
| 记了摘要开销后"单次对话费用变高" | 这是**本来就在花的钱**，只是过去没记。首次上线建议主动告知用户，避免误解成"变贵了" |
| 不改 `messages` 表 | 避免无迁移工具下的 ALTER 风险，代价是老数据的模型名缺失 |

---

## 十、需要你拍板的三件事

1. **图表**：自绘 SVG（推荐，零依赖）还是引入 ECharts？
2. **历史数据**：要不要回填现有 `messages` 里的 token 数？（回填后总量好看，但模型名是 unknown）
3. **单价**：你能提供 DeepSeek 当前准确报价吗？没有的话我按示例值占位，并在页面标注"单价待校准"。
