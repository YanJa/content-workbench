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

from app.core.pricing import (
    CST,
    canonical_model,
    estimate_cost,
    is_known,
    is_peak_hour,
    label_of,
    priced_as,
)
from app.models.conversation import Conversation
from app.models.usage import LlmUsage


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
) -> LlmUsage:
    """记录一次调用的用量。

    is_peak 在这里定格（按 created_at 当时的时段算），
    避免事后查账时因为"现在是低谷"而把白天的高峰调用算便宜了。
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
    """按当时定格的 is_peak 算费用，保证历史数据不随时间漂移。"""
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
    """把一组记录聚合成统计块。"""
    cost = sum(cost_of(r) for r in rows)
    return {
        "calls": len(rows),
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
    }
