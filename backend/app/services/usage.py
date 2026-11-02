"""用量服务：落库 + 聚合 + 计价。

聚合为什么放在 Python 里做，而不是写 SQL GROUP BY：
  1. 数据量小（日均几百到几千行），Python 聚合完全够快
  2. 按天分组要按**北京时间**切，SQLite 得写 strftime('%Y-%m-%d', created_at,'unixepoch','+8 hours')，
     换成 PostgreSQL 又是另一套方言 —— 放在 SQL 里就是给未来埋坑
"""
from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import pricing
from app.core.pricing import (
    CST,
    canonical_model,
    estimate_cost,
    is_free,
    is_known,
    is_peak_hour,
    label_of,
    priced_as,
)
from app.models.conversation import Conversation
from app.models.usage import LlmUsage


# ------------------------------------------------------------------ 计费性质
def billing_of(model: str, provider_key: str = "") -> str:
    """这次调用算哪种账：**免费 / 付费 / 不知道**。

    顺序很讲究，别改：
      1. 单价表明确标了免费 → free
      2. 单价表登记过价 → paid
      3. 模型池里标了 free（CF、火山免费档这些）→ free
         —— 池是免费模型的权威来源：新接入的免费模型不会第一时间进单价表，
            但它在池里已经挂着 billing=free 了。
      4. 模型没进池，但它家服务商是「免费通道」（CF）→ 也算 free
      5. 池里标了 paid 但单价表没价 → 还是 unknown（宁可按兜底价估高，
         也不能因为「不知道多少钱」就记成免费 —— 账目偏低的后果更严重）
      6. 都没有 → unknown
    """
    name = canonical_model(model or "")
    kind = pricing.billing_of(name)
    if kind != "unknown":
        return kind
    try:
        from app.services import model_pool

        pool_billing = model_pool.billing_of(name, provider_key)
    except Exception:  # noqa: BLE001  — 记账不能因为池不可用而失败
        pool_billing = ""
    if pool_billing == "free":
        return "free"
    return "unknown"


# ------------------------------------------------------------------ 落库
async def record(
    db: AsyncSession,
    *,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    task_type: str = "",
    conversation_id: str | None = None,
    message_id: str | None = None,
    owner: str = "default",
    scene: str = "chat",
    latency_ms: int = 0,
    is_summary: bool = False,
    estimated: bool = False,
    created_at: int | None = None,
    cred_owner: str = "system",
    billing: str | None = None,
    provider_key: str = "",
) -> LlmUsage:
    """记录一次调用的用量。

    is_peak 在这里定格（按 created_at 当时的时段算），
    避免事后查账时因为"现在是低谷"而把白天的高峰调用算便宜了。

    billing 同理定格：调用当时这个模型是免费的就是免费的，
    之后管理员把它改成付费，不该让上个月的账单跟着涨价。
    """
    ts = created_at or int(time.time())
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    row = LlmUsage(
        conversation_id=conversation_id,
        message_id=message_id,
        owner=owner or "default",
        scene=scene or "chat",
        model=canonical_model(model),
        task_type=task_type or "",
        cred_owner=cred_owner or "system",
        billing=(billing or billing_of(model, provider_key)),
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        reasoning_tokens=int(reasoning_tokens or 0),
        cached_tokens=int(cached_tokens or 0),
        total_tokens=total,
        latency_ms=int(latency_ms or 0),
        is_peak=is_peak_hour(ts),
        is_summary=bool(is_summary),
        estimated=bool(estimated),
        created_at=ts,
    )
    db.add(row)
    await db.flush()
    return row


async def backfill_free_billing(db: AsyncSession, limit: int = 20_000) -> int:
    """给老行补上「这笔是免费的」。

    只补能证明的那部分：这个模型现在明确标着 billing=free。判定来源有两处
    （任一说 free 就算）：
      1. 模型池 catalog 的 billing 字段
      2. 单价表 PRICING 里登记的免费模型（v1.9.9 起免费模型直接登记在 PRICING，
         不一定进池；glm-4-flash / glm-4.7-flash / 各 CF 免费模型都在这）

    补不了的（模型没进池也没登记、或者本来就是付费）保持空串 / unknown ——
    空串 = 按兜底价估，也就是维持加列之前的口径；unknown = 仍按兜底价估并
    继续在前端标 ⚠。**不回溯改任何金额**，只是把「免费」这个事实补记上去，
    免得同一行既标着免费又显示一笔钱。

    历史里存成 `unknown` 的行一并订正：那是「当初没登记」时落的，现在既然
    证明是免费，就该改判 free（否则 warning 一直挂着、费用也一直被估成钱）。
    """
    from sqlalchemy import select as _select

    try:
        from app.services import model_pool

        snap = model_pool.snapshot()
        free_models = {
            str(e.get("model_id") or "")
            for e in snap.catalog.values()
            if e.get("billing") == "free"
        }
    except Exception:  # noqa: BLE001
        free_models = set()
    # 单价表也认免费：免费模型不一定进池，但登记在 PRICING 里就是免费。
    free_models |= {m for m in pricing.PRICING if pricing.is_free(m)}
    if not free_models:
        return 0

    rows = (
        await db.execute(
            _select(LlmUsage)
            .where(
                LlmUsage.billing.in_(["", "unknown"]),
                LlmUsage.model.in_(list(free_models)),
            )
            .limit(limit)
        )
    ).scalars().all()
    for r in rows:
        r.billing = "free"
    if rows:
        await db.flush()
    return len(rows)


def make_sink(db: AsyncSession, **ctx: Any) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """造一个埋点回调，交给 LLM 层在每次调用结束时触发。

    ctx 里放调用方才知道的上下文（conversation_id / scene / owner / is_summary …），
    LLM 层只知道 token 数和耗时。两边在 record() 里合并。

    这里 commit 而不是 flush：用量是「账单」，宁可多一次提交，
    也不能因为对话流中途报错导致这一笔丢了。
    """

    async def sink(payload: dict[str, Any]) -> None:
        await record(
            db,
            model=payload.get("model", ""),
            task_type=payload.get("task", ""),
            # 模型池会带上这次用的是谁的 Key（平台凭证 / 用户个人凭证）
            cred_owner=payload.get("cred_owner") or "system",
            # 池里解析出来的模型自带 billing（free/paid）；走 .env 通道时为 ""，
            # 由 record() 回落去查计价表 / 池快照。
            billing=payload.get("billing") or None,
            provider_key=payload.get("provider_key") or "",
            prompt_tokens=payload.get("prompt_tokens", 0),
            completion_tokens=payload.get("completion_tokens", 0),
            reasoning_tokens=payload.get("reasoning_tokens", 0),
            cached_tokens=payload.get("cached_tokens", 0),
            latency_ms=payload.get("latency_ms", 0),
            estimated=bool(payload.get("estimated", False)),
            **ctx,
        )
        await db.commit()

    return sink


def total_tokens_fallback(prompt: int, completion: int) -> int:
    """服务商没给 total 时自己算一个。"""
    return int(prompt or 0) + int(completion or 0)


def cost_of(row: LlmUsage) -> float:
    """按当时定格的 is_peak 与 billing 算费用，保证历史数据不随时间漂移。

    免费模型直接 0 元 —— 不是「单价乘出来刚好是 0」，是**性质上就不要钱**。
    （顺带避开另一个坑：免费模型没登记单价时，兜底价会把它算成 DeepSeek 的钱。）
    """
    if (getattr(row, "billing", "") or "") == "free":
        return 0.0
    return estimate_cost(
        row.model,
        row.prompt_tokens,
        row.completion_tokens,
        row.cached_tokens,
        is_peak=row.is_peak,
    )


# ------------------------------------------------------------------ 查询
def _base_query(
    *,
    owner: str = "default",
    days: int | None = None,
    conversation_id: str | None = None,
    model: str | None = None,
    scene: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> Select:
    q = select(LlmUsage).where(LlmUsage.owner == (owner or "default"))
    if conversation_id:
        q = q.where(LlmUsage.conversation_id == conversation_id)
    if model:
        q = q.where(LlmUsage.model == model)
    if scene:
        q = q.where(LlmUsage.scene == scene)
    if start:
        q = q.where(LlmUsage.created_at >= start)
    if end:
        q = q.where(LlmUsage.created_at <= end)
    if days and not start:
        q = q.where(LlmUsage.created_at >= int(time.time()) - days * 86400)
    return q.order_by(LlmUsage.created_at.desc())


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")


async def _fetch(db: AsyncSession, q: Select) -> list[LlmUsage]:
    rows = await db.execute(q)
    return list(rows.scalars().all())


def _agg(rows: list[LlmUsage]) -> dict:
    """把一组记录聚合成统计块。

    免费调用单列出来：费用是 0，但**成本不是 0**（吃的是每日额度），
    只给一个「花了 0 元」会让人以为这些调用不存在。
    """
    cost = sum(cost_of(r) for r in rows)
    free_rows = [r for r in rows if (getattr(r, "billing", "") or "") == "free"]
    return {
        "calls": len(rows),
        "free_calls": len(free_rows),
        "free_tokens": sum(r.total_tokens for r in free_rows),
        "prompt_tokens": sum(r.prompt_tokens for r in rows),
        "completion_tokens": sum(r.completion_tokens for r in rows),
        "reasoning_tokens": sum(r.reasoning_tokens for r in rows),
        "cached_tokens": sum(r.cached_tokens for r in rows),
        "total_tokens": sum(r.total_tokens for r in rows),
        "cost": round(cost, 6),
        "avg_latency_ms": round(sum(r.latency_ms for r in rows) / len(rows)) if rows else 0,
    }


async def summary(
    db: AsyncSession,
    *,
    owner: str = "default",
    days: int | None = None,
    conversation_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> dict:
    """总量概览 + 按天/按模型/按场景的分组序列。

    一次取数、三种分组都在内存里切，避免同一个页面打三个接口。
    """
    rows = await _fetch(
        db,
        _base_query(
            owner=owner,
            days=days,
            conversation_id=conversation_id,
            start=start,
            end=end,
        ),
    )

    by_day: dict[str, list[LlmUsage]] = defaultdict(list)
    by_model: dict[str, list[LlmUsage]] = defaultdict(list)
    by_scene: dict[str, list[LlmUsage]] = defaultdict(list)
    for r in rows:
        by_day[_day_key(r.created_at)].append(r)
        by_model[r.model or "unknown"].append(r)
        by_scene[r.scene or "chat"].append(r)

    def series(bucket: dict[str, list[LlmUsage]], *, is_model: bool = False) -> list[dict]:
        out = []
        for key, items in bucket.items():
            stat = _agg(items)
            stat["key"] = key
            if is_model:
                # 换了新模型但没登记单价时，这里会给出 known=false，
                # 前端据此提示「按兜底价估算」，而不是默默算错还装作没事
                stat["label"] = label_of(key)
                stat["known"] = is_known(key)
                stat["priced_as"] = priced_as(key)
                # 同一个 key 下 billing 一致（都是同一个模型），取第一行即可
                stat["billing"] = (
                    getattr(items[0], "billing", "") or ""
                ) or billing_of(key)
            out.append(stat)
        out.sort(key=lambda x: x["key"])
        return out

    return {
        "total": _agg(rows),
        "by_day": series(by_day),
        "by_model": series(by_model, is_model=True),
        "by_scene": series(by_scene),
    }


async def records(
    db: AsyncSession,
    *,
    owner: str = "default",
    limit: int = 50,
    offset: int = 0,
    days: int | None = None,
    conversation_id: str | None = None,
    model: str | None = None,
    scene: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[list[dict], int]:
    """明细分页。返回 (条目, 总数)。"""
    q = _base_query(
        owner=owner,
        days=days,
        conversation_id=conversation_id,
        model=model,
        scene=scene,
        start=start,
        end=end,
    )
    rows = await _fetch(db, q)
    page = rows[offset : offset + limit]
    return [_row_out(r) for r in page], len(rows)


async def top_conversations(
    db: AsyncSession, *, owner: str = "default", days: int | None = 30, limit: int = 10
) -> list[dict]:
    """消耗最高的会话。带上标题，方便直接从用量页点进去看。"""
    rows = await _fetch(db, _base_query(owner=owner, days=days))
    bucket: dict[str, list[LlmUsage]] = defaultdict(list)
    for r in rows:
        if r.conversation_id:
            bucket[r.conversation_id].append(r)

    # 顺手把标题取出来，避免前端再打一次接口
    titles: dict[str, str] = {}
    if bucket:
        convs = await db.execute(
            select(Conversation.id, Conversation.title).where(
                Conversation.id.in_(list(bucket.keys()))
            )
        )
        titles = dict(convs.all())

    out = []
    for conv_id, items in bucket.items():
        stat = _agg(items)
        stat["conversation_id"] = conv_id
        stat["title"] = titles.get(conv_id, "（会话已删除）")
        out.append(stat)
    out.sort(key=lambda x: x["cost"], reverse=True)
    return out[:limit]


def _row_out(r: LlmUsage) -> dict:
    return {
        "id": r.id,
        "conversation_id": r.conversation_id,
        "message_id": r.message_id,
        "scene": r.scene,
        "model": r.model,
        "task_type": r.task_type,
        "cred_owner": r.cred_owner or "system",
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "reasoning_tokens": r.reasoning_tokens,
        "cached_tokens": r.cached_tokens,
        "total_tokens": r.total_tokens,
        "latency_ms": r.latency_ms,
        "is_peak": r.is_peak,
        "is_summary": r.is_summary,
        "estimated": r.estimated,
        "cost": cost_of(r),
        "created_at": r.created_at,
        "model_label": label_of(r.model),
        "price_known": is_known(r.model),
        "billing": (getattr(r, "billing", "") or "") or billing_of(r.model),
    }
