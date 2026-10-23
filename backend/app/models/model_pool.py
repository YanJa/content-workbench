"""模型池：服务商、模型条目、功能绑定、额度计数器。

--------------------------------------------------------------------------
为什么要有这套表
--------------------------------------------------------------------------
改造前模型只有两个来源：`DEEPSEEK_*` 与 `CLOUDFLARE_*` 两组环境变量，
provider 是散落在十处的硬编码字符串 "deepseek" / "cloudflare"，
MODEL_ROUTES 把模型名、温度、输出上限全写死在代码里。

用户要接国内多家厂商的免费模型（智谱 / 硅基流动 / 千帆 / 混元 / 火山 / 百炼 …），
还要混着自己的付费模型用，每个功能用不同模型，每个模型带额度 ——
「加一个厂商 = 改一遍代码」这条路走到头了。

--------------------------------------------------------------------------
四张表各自回答一个问题
--------------------------------------------------------------------------
  ModelProvider     谁在提供模型（凭证 + 端点 + 账号级额度）
  ModelProviderKey  某个用户是否用自己的 Key 顶掉全局 Key（可选覆盖）
  ModelCatalog      池里有哪些模型（能力、参数、计费、额度、熔断）
  ModelBinding      每个功能槽用哪个模型、备选链是什么
  ModelQuotaUsage   额度计数器（周期累加，不是事实表）

--------------------------------------------------------------------------
两条刻意的设计取舍
--------------------------------------------------------------------------
1. **个人 Key 只存 api_key，其它一律继承全局定义。**
   不是「同表加一行 owner=user_id 的记录」，那会带来「base_url 是继承还是
   覆盖」「enabled=False 是不覆盖还是禁用」「管理员改了 base_url 个人行漂移」
   三个说不清的问题。拆一张只放 Key 的表，继承漂移从数据结构上就不可能发生：
   删掉这一行 = 回退全局。

2. **额度既有 model 级也有 provider 级，靠 ModelCatalog.quota_scope 切。**
   这不是过度设计：Cloudflare 免费额度是 **10,000 Neurons/天、账号级、跨模型
   共享、00:00 UTC 重置**（官方定价页）。按 model 分桶等于把这一万额度放大 N 倍，
   「免费额度用尽自动降级」在 CF 上永远触发不了 —— 而 CF 恰恰是免费模型的大头。
   同理，火山引擎 200 万 token/天也是账号级日刷新。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex


class ModelProvider(Base):
    """服务商（全局，管理员维护）。

    owner 恒为 "system"：凭证是系统级资源，不按用户隔离（与知乎 Access Secret
    同款口径，见 services/zhihu.py 的 _SECRET_OWNER）。个人想用自己的 Key 走
    ModelProviderKey，不在这里加行。

    kind:
      - "openai"：OpenAI 兼容端点。国内绝大多数厂商都能用同一套 SDK 调，
        这是「加一个厂商不用写代码」的基础；
      - "cloudflare"：Workers AI，端点形如 /client/v4/accounts/{id}/ai/v1，
        且不支持 function calling、不认 stream_options（带了 400）。

    账号级额度（daily_* / monthly_* / rpm_*）：
      给 CF「10k Neurons/天」这类跨模型共享的配额准备。留空 = 不限制。
    """

    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="system", index=True)
    # 短标识：zhipu / siliconflow / cloudflare …。解析时靠它把模型条目和服务商对上
    key: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(16), default="openai")
    base_url: Mapped[str] = mapped_column(String(300), default="")
    # 明文存本机 SQLite，接口响应一律脱敏 —— 与 WeChatAccount.secret 同款约定
    api_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")

    # 账号级额度护栏。None = 不限制（不是 0 次 —— 0 会被当成「已用尽」直接熔断）
    daily_calls_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 每分钟请求数。免费档真正先撞的往往是 QPS 而不是总量（普遍每分钟 20 次、
    # 并发 2），不设这个的话「自动切换」只是把 429 从一家搬到另一家
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 日额度在哪儿重置：CF 是 00:00 UTC，国内厂商按北京时间
    quota_reset_tz: Mapped[str] = mapped_column(String(8), default="cst")

    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)

    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_provider_owner_key"),
        Index("ix_provider_enabled", "enabled"),
    )


class ModelProviderKey(Base):
    """个人凭证覆盖（可选）。

    只放 api_key —— base_url、kind、模型清单永远取自 ModelProvider 的全局定义。
    用户填了自己的 Key，就烧自己的额度（cred_owner = 该用户），不占公共额度桶。
    """

    __tablename__ = "model_provider_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    provider_key: Mapped[str] = mapped_column(String(32), default="")
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    api_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 上次连通性测试通过的时间；测试失败只提示，不自动禁用
    verified_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)

    __table_args__ = (UniqueConstraint("provider_key", "owner", name="uq_pkey"),)


class ModelCatalog(Base):
    """模型池里的一条模型。

    protocol 决定怎么调它：
      - openai_chat      chat.completions（流式/非流式）
      - openai_images    images.generate（生图）
      - openai_embeddings embeddings.create（向量化）
      - cf_ai_run_image  Cloudflare REST /ai/run/{model}（CF 生图，非 SDK）
      - cf_ai_run_embed  Cloudflare REST /ai/run/{model}（CF 向量化）

    capabilities 是数组而不是单值枚举：deepseek-flash 同时能 chat 和 vision，
    单值表达不了。

    supports_stream_usage=False 的条目（CF）流式时不带 stream_options ——
    这条以前是 `if provider == "cloudflare"` 的字符串判断，新接厂商必然漏。

    熔断字段：一个 401 的 Key 如果不记下来，之后每一次对话都会先撞它一次，
    比没有模型池时更慢。
    """

    __tablename__ = "model_catalog"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    provider_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    model_id: Mapped[str] = mapped_column(String(120), default="")  # 厂商原始模型名
    label: Mapped[str] = mapped_column(String(120), default="")  # 展示名
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    protocol: Mapped[str] = mapped_column(String(24), default="openai_chat")

    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # None = 回落到 MODEL_ROUTES[task] 里那些调过的值
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(nullable=True)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_stream_usage: Mapped[bool] = mapped_column(Boolean, default=True)
    # 仅向量化模型：维度。换 embedding 模型时靠它拦维度不一致
    dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    billing: Mapped[str] = mapped_column(String(8), default="free")  # free | paid
    # 元 / 百万 tokens。免费模型填 0；留 None 表示「沿用计价表或按兜底价估算」
    price_in: Mapped[float | None] = mapped_column(nullable=True)
    price_out: Mapped[float | None] = mapped_column(nullable=True)

    # 额度桶口径：model = 这个模型自己一份；provider = 与账号下其它模型共享
    quota_scope: Mapped[str] = mapped_column(String(16), default="model")
    daily_calls_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 熔断：最近一次错误 + 到这个时间戳之前不再选它
    last_error: Mapped[str] = mapped_column(Text, default="")
    disabled_until: Mapped[int] = mapped_column(Integer, default=0)

    sort: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)

    __table_args__ = (
        UniqueConstraint("provider_key", "model_id", name="uq_catalog_model"),
        Index("ix_catalog_enabled", "enabled"),
    )


class ModelBinding(Base):
    """功能槽 → 模型（+ 备选链）。

    8 个槽：chat / summary / layout / layout_tags / xhs_format /
    image_prompt_translate / image / embedding。

    全局单套（管理员配），不做 per-user 绑定 —— 功能用哪个模型是系统策略，
    不是个人偏好；个人能覆盖的是凭证（ModelProviderKey）。
    """

    __tablename__ = "model_bindings"

    feature: Mapped[str] = mapped_column(String(40), primary_key=True)
    primary_model_id: Mapped[str] = mapped_column(String(32), default="")
    # 备选模型 id 数组，按顺序尝试
    fallbacks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)


class ModelQuotaUsage(Base):
    """模型池额度计数器（周期累加，不是事实表）。

    为什么不直接聚合 llm_usage 表：
      1. Cloudflare 流式拿不到 usage（llm.py 已注明，且有测试固化），
         token 额度在免费模型上永远不触发；
      2. 生图按张计费，token 口径没意义；
      3. llm_usage 是只增不改的事实表，热路径上 SUM() 太重。

    bucket 是 provider_key 或 model_id（由 quota_scope 决定）；
    cred_owner 区分「烧的是平台 Key 还是某人自己的 Key」。
    """

    __tablename__ = "model_quota_usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    bucket: Mapped[str] = mapped_column(String(120), default="")
    cred_owner: Mapped[str] = mapped_column(String(64), default="system")
    period: Mapped[str] = mapped_column(String(8), default="day")  # day | month
    period_key: Mapped[str] = mapped_column(String(16), default="")  # 2026-10-24 / 2026-10
    calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    images: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)

    __table_args__ = (
        UniqueConstraint("bucket", "cred_owner", "period", "period_key", name="uq_mq_period"),
        Index("ix_mq_bucket_period", "bucket", "period", "period_key"),
    )
