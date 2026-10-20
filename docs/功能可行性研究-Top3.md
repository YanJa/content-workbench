# Top3 功能可行性研究（效果回收 / 内容日历+定时发布 / 素材库）

> 研究目的：评估上一轮梳理出的 Top3 建议（效果回收、内容日历+定时发布、素材库/收藏夹）在当前架构下的落地可行性、工作量与风险，给出推荐落地范围。
> 研究日期：2026-10-20

---

## 0. 决定可行性的两个硬约束（务必先读）

### 约束 A：数据库无 Alembic 迁移

当前是 **SQLite + 异步 SQLAlchemy**（`backend/app/db/session.py`）。建表靠启动时 `Base.metadata.create_all`；但**已有表加列**不会自动生效，必须在 `session.py` 的 `_SQLITE_COLUMN_PATCHES` 字典里注册一行，由 `_patch_sqlite_columns()` 在启动时 `ALTER TABLE ADD COLUMN` 补上。

- **加新表**：模型继承 `Base` 即可，`create_all` 自动建——零迁移负担。
- **给已有表加列**：必须在 `session.py` 的 patch 表登记，且**非空列必须带 `DEFAULT`**（否则老库 `ALTER TABLE` 直接报错）。
- 改列类型 / 删列 / 加索引：这套机制不支持，需要单独写迁移脚本。

**结论**：三个方案里凡是"新建表"的（素材库、通知中心）都最省事；凡是"给 `contents`/`queue_items` 加列"的（定时、效果回收）都要走 patch 登记，且要留意非空列默认值。

### 约束 B：公众号刻意"不自动群发"

`services/wechat.py` 文件头注释明确：

> 不做自动群发。草稿箱接口（draft/add）对个人订阅号同样开放，合规可用；**发布接口（freepublish）个人号已被回收，且自动发布本身违规**。所以终态是：自动进草稿 → 人工核对 → 人工群发。

也就是说，当前只实现了 `add_draft`（推草稿箱），**没有群发、没有图文数据读取接口**。`queue_items` 的 `published_at` 是"用户手动标记完成"的时间戳，**不是真实群发时间**；`status=published` 也只是人工标记，不等于公众号已群发。

**结论**：任何依赖"内容已在公众号真实群发"的能力（自动定时群发、自动拉阅读量），在当前合规架构下直接受阻，要么降级为"人工"，要么推翻约束 B（不推荐，涉合规）。

---

## 1. 素材库 / 收藏夹 —— ✅ 强烈推荐，优先做

### 可行性：最高
完全不触碰约束 A/B 的雷区：新建表走 `create_all`，与公众号无关，与现有"生产 → 发布"主链路正交。

### 现状缺口
知乎热榜灵感、对话产出的金句、配图、选题现在散落在各页面，无法沉淀复用。系统里 `ImageManager` 只管临时图，没有"素材/收藏"概念。

### 方案设计

**数据模型（新表 `materials`，零迁移成本）**
```python
class Material(Base):
    __tablename__ = "materials"
    id: str = mapped_column(String(36), primary_key=True)
    owner: str = mapped_column(String(64), index=True)
    type: str = mapped_column(String(16), index=True)   # quote/idea/image/link/clip
    title: str = mapped_column(String(200), default="")
    content: str = mapped_column(Text, default="")       # 文本正文；图片存相对路径
    tags: dict | None = mapped_column(JSON, nullable=True)
    source_type: str | None = mapped_column(String(16))  # chat/queue/zhihu/manual
    source_id: str | None = mapped_column(String(36))
    created_at: int = mapped_column(default=_now)
```

**后端**：新建 `routers/materials.py` —— CRUD（增删改查）、按 `type`/`tag`/关键词过滤、从某内容"一键收藏"（接收 source_type+source_id）。权限默认登录即可用，无需新增 RBAC 点。

**前端**：`api/materials.ts` + `stores/material.ts` + `views/MaterialView.vue`（列表/标签筛选/预览）；侧栏与移动底栏加入口；在 ChatView 成品卡、QueueView 卡片、HotInspiration 卡片上加"收藏到素材库"按钮（调收藏接口，带 source_id）。

### 工作量：S–M（约 1–2.5 天，单人）
- 后端 CRUD + 模型：0.5–1 天
- 前端页面 + store + 入口 + 3 处收藏按钮：1–1.5 天

### 风险：低
无合规风险、无架构冲突、无破坏性迁移。

---

## 2. 内容日历 + 定时发布 —— ✅ 推荐，但"定时"要重新定义

### 可行性：中高（取决于"定时"的定义）

约束 B 把"自动定时群发到公众号"这条路堵死了。所以本方案必须拆成两层：

#### 2.1 内容日历 / 排期视图 —— ✅ 优先、安全
纯展示层，不涉及真实群发。

- **加列**：给 `queue_items`（或 `contents`）加 `scheduled_at: int | None`，在 `session.py` patch 表登记（可空，无 DEFAULT 风险）。
- **后端**：`PATCH /queue/{id}/schedule`（设置/清除计划时间）、`GET /calendar?from&to`（返回区间排期）。
- **前端**：`views/CalendarView.vue`（naive 的 `NCalendar` 月视图 + 排期卡片）；支持点选/拖拽改期；侧栏入口。
- 工作量：M（约 1.5–2.5 天）。

#### 2.2 "到点提醒" —— ⚠️ 可做，但依赖通知中心
到 `scheduled_at` 时提醒"该去公众号群发了"，而不是自动群发。

- 需要一个轻量后台扫描：FastAPI 启动时起一个 `asyncio` 循环，周期性扫 `scheduled_at` 临近且未发布的项，往**通知中心**推送提醒。
- 问题：**通知中心本身是 Top3 之外的 P1 功能，还没建**。所以 2.2 应排在"通知中心"之后，或第一版先用邮箱/企业微信 webhook 兜底。
- 工作量：依赖通知中心，建议作为第二步。

#### 2.3 自动定时群发 —— ❌ 不推荐
需要给 `wechat.py` 加群发接口 + 定时触发，直接推翻约束 B 的合规设计，且个人订阅号根本没有群发/数据接口权限。**本期不做。**

### 推荐落地：先做 2.1（日历+排期），2.2 等通知中心就绪后接上。

---

## 3. 效果回收（阅读量/点赞/转发/涨粉） —— ⚠️ 自动不可行，人工回填可行

### 可行性：低（自动）/ 中（人工）

这是三个里**受约束 B 打击最重**的。要自动读公众号阅读量，前提全部卡死：
1. 内容必须**已在公众号真实群发**——而当前只推草稿箱、不群发；
2. 微信图文统计数据（`getuserread` 等）**群发后次日**才出，且需 `access_token` + 数据接口权限；
3. 个人订阅号**没有**图文群发数据接口权限（服务号才有，且需认证）。

而小红书 / 抖音侧**无任何公开数据接口**，自动回收从原理上就不可能。

#### 3.1 人工回填 —— ✅ 推荐，低成本
- **加列**：`queue_items` 加 `metrics: JSON`（read/like/share/fav/fans，可空），在 patch 表登记。
- **后端**：`PATCH /queue/{id}/metrics`（运营从公众号后台看到数据后手动填）。
- **前端**：QueueView 详情/抽屉里加"数据回填"表单；列表可展示阅读量列。
- 工作量：S（约 0.5–1 天）。
- 价值：立刻把"发布后闭环"补上 70%，且完全不依赖外部接口与合规。

#### 3.2 自动拉取（仅服务号） —— ❌ 本期不做
需：① 推翻约束 B 支持"授权并群发"；② 对接 `getuserread` 次日定时拉取；③ 仅服务号可用。工程量大、合规风险高、覆盖窄。**列为二期候选，且需产品先确认是否要改"不自动群发"的合规底线。**

---

## 4. 落地建议（按性价比排序）

| 优先级 | 功能 | 范围 | 工作量 | 可行性 |
|---|---|---|---|---|
| **1** | 素材库/收藏夹 | 新表 + CRUD + 页面 + 3 处收藏入口 | S–M | ✅ 最高，零冲突 |
| **2** | 内容日历+排期 | `scheduled_at` 列 + 日历页 + 设计划时间接口 | M | ✅ 安全，绕开群发 |
| **3** | 效果回收（人工回填） | `metrics` 列 + 回填接口 + 表单 | S | ✅ 低成本，先补闭环 |
| — | 到点提醒 | 依赖通知中心，建议第二步 | M | ⚠️ 需先建通知中心 |
| ✗ | 自动定时群发 / 自动阅读量 | 推翻"不自动群发"合规设计 | L | ❌ 不推荐 |

**一句话结论**：素材库立刻能做、收益最高；内容日历做"展示+排期"而不是"自动发"；效果回收先用人工回填把闭环补上，自动拉取等合规与接口条件成熟再说。三者都不需要引入 Alembic——加表零成本，加列走现有 patch 登记即可。

### 共性落地清单（实现时照着做）
- **新表**：在 `app/models/` 建模型，`import app.models` 已在 `init_db` 里触发 `create_all`，无需额外动作。
- **加列**：改 `app/db/session.py` 的 `_SQLITE_COLUMN_PATCHES`，非空列务必带 `DEFAULT`。
- **新页面**：`router/index.ts` 加路由；`AppShell.vue` 侧栏 + 移动底栏加入口；若需权限隔离，在 `core/rbac.py` 注册新权限点（否则默认登录可用）。
- **前端范式**：沿用现有 `api/*.ts`（axios 封装）+ `stores/*.ts`（pinia）+ `views/*.vue`，无需新框架。
