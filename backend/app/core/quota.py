"""功能用量配额（计数与拦截）。

口径
----
- **提交时预扣**：五个受控功能全是异步/流式的，等「成功」再记账等于允许反复
  提交失败任务零成本刷爆 GPU 和 token。详见 models/quota.py 的说明。
- **只有推送草稿明确失败才退还**（`refund`）—— 它是唯一同步能判成败的动作。
- **计数用独立 session 立即提交**，不搭业务事务的车：否则端点一报错回滚，
  扣减就跟着撤销了，而 SSE 那种已经开流的又扣了 —— 同一个动作两种结果。

并发安全
--------
累加走**单语句条件 UPDATE**（`WHERE count < limit`），不是「读出来 +1 再写回」
—— 后者在并发下会丢更新，配额就成了摆设。首次使用则 INSERT，撞唯一约束
（并发首插）就退回条件 UPDATE 重试一次。

周期
----
`day` / `month`，period_key 按 **CST** 生成（北京时间）—— 用 UTC 的话
「今日用量」在早上 8 点前是对不上的。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.logging import logger
from app.core.pricing import CST
from app.db.session import SessionLocal
from app.models.quota import QuotaUsage

# 受配额管控的功能。**这是 5 个「会花钱/会占资源」的动作**，与权限点
# （core/rbac.py 的 PERMISSIONS）是两张表：生图、封面只受权限管控，不计次。
FEATURES: dict[str, str] = {
    "chat": "AI 对话",
    "video": "视频生成",
    "layout": "排版",
    "voice_clone": "声音复刻",
    "wechat_push": "推送草稿",
}

PERIODS = ("day", "month")
PERIOD_LABEL = {"day": "每日", "month": "每月"}

# limit 取这些值都表示「不限」：缺失、0、负数。绝不能把「没配」当成 0 次 ——
# 那是 fail-closed，新功能一上线全体用户就用不了了。
UNLIMITED = (None, 0, -1)


def _now() -> int:
    return int(time.time())


def period_key(period: str, now: int | None = None) -> str:
    """当前周期的键：'2026-10-17' / '2026-10'（CST）。"""
    d = datetime.fromtimestamp(now if now is not None else _now(), CST)
    return d.strftime("%Y-%m-%d") if period == "day" else d.strftime("%Y-%m")


def reset_at(period: str, now: int | None = None) -> int:
    """下次重置的时间戳（CST 零点）。前端据此显示「明日 00:00 重置」。"""
    d = datetime.fromtimestamp(now if now is not None else _now(), CST)
    if period == "day":
        nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = (d.replace(day=1) + timedelta(days=32)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return int(nxt.timestamp())


def normalize(conf: dict | None) -> tuple[int | None, str]:
    """把一条配额配置归一成 (limit, period)。limit=None 表示不限。"""
    if not conf:
        return None, "day"
    limit = conf.get("limit")
    period = conf.get("period") or "day"
    if period not in PERIODS:
        period = "day"
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    if limit is None or limit <= 0:
        return None, period
    return limit, period


# ---------------------------------------------------------------- 计数
async def _bump(owner: str, feature: str, period: str, key: str, limit: int | None) -> int:
    """计数 +1，返回用后的值。超限（limit 非空且已达）抛 429。"""
    now = _now()
    async with SessionLocal() as db:
        stmt = (
            update(QuotaUsage)
            .where(
                QuotaUsage.owner == owner,
                QuotaUsage.feature == feature,
                QuotaUsage.period == period,
                QuotaUsage.period_key == key,
            )
            .values(count=QuotaUsage.count + 1, updated_at=now)
        )
        if limit is not None:
            stmt = stmt.where(QuotaUsage.count < limit)

        res = await db.execute(stmt)
        if res.rowcount == 1:
            await db.commit()
            return await _current(db, owner, feature, period, key)

        # rowcount == 0：这一行还不存在（首次使用），或者已经用完了
        row = (
            await db.execute(
                select(QuotaUsage).where(
                    QuotaUsage.owner == owner,
                    QuotaUsage.feature == feature,
                    QuotaUsage.period == period,
                    QuotaUsage.period_key == key,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            if limit is not None and limit <= 0:
                raise HTTPException(429, f"{FEATURES.get(feature, feature)}次数已用完（0/{limit}）")
            db.add(
                QuotaUsage(
                    owner=owner,
                    feature=feature,
                    period=period,
                    period_key=key,
                    count=1,
                    updated_at=now,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                # 并发下另一个请求先插了 → 退回条件 UPDATE 抢一次
                await db.rollback()
                return await _bump(owner, feature, period, key, limit)
            return 1

        # 行存在却没更新成功 = 已到上限
        raise HTTPException(
            429,
            f"{PERIOD_LABEL.get(period, '')}{FEATURES.get(feature, feature)}次数已用完"
            f"（{row.count}/{limit}），{_reset_hint(period)}",
        )


def _reset_hint(period: str) -> str:
    ts = reset_at(period)
    d = datetime.fromtimestamp(ts, CST)
    return f"{d.strftime('%m-%d %H:%M')} 重置" if period == "day" else f"{d.strftime('%m-%d')} 重置"


async def _current(db, owner: str, feature: str, period: str, key: str) -> int:
    row = (
        await db.execute(
            select(QuotaUsage.count).where(
                QuotaUsage.owner == owner,
                QuotaUsage.feature == feature,
                QuotaUsage.period == period,
                QuotaUsage.period_key == key,
            )
        )
    ).scalar_one_or_none()
    return int(row or 0)


# ---------------------------------------------------------------- 对外
async def usage_of(owner: str, quotas: dict | None = None, db=None) -> dict:
    """只读：各功能的用量快照。给 /auth/me 用，前端据此置灰按钮。

    `db` 可传可不传：传了就用它（省一次连接），不传自己开一个。
    """
    own_db = db is None
    if own_db:
        db = SessionLocal()  # type: ignore[assignment]
    try:
        out: dict[str, dict] = {}
        for feature in FEATURES:
            limit, period = normalize((quotas or {}).get(feature))
            key = period_key(period)
            used = await _current(db, owner, feature, period, key)
            out[feature] = {
                "label": FEATURES[feature],
                "used": used,
                "limit": limit,
                "period": period,
                "unlimited": limit is None,
                "left": None if limit is None else max(0, limit - used),
                "reset_at": reset_at(period),
            }
        return out
    finally:
        if own_db:
            await db.close()  # type: ignore[union-attr]


async def consume(owner: str, feature: str, conf: dict | None = None) -> None:
    """扣一次。超限抛 429（中文文案含「已用完 / 已用 / 重置时间」）。"""
    limit, period = normalize(conf)
    await _bump(owner, feature, period, period_key(period), limit)


async def refund(owner: str, feature: str, conf: dict | None = None) -> None:
    """退还一次（仅用于「明确失败」的同步动作，如推送草稿失败）。"""
    _, period = normalize(conf)
    key = period_key(period)
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(QuotaUsage).where(
                    QuotaUsage.owner == owner,
                    QuotaUsage.feature == feature,
                    QuotaUsage.period == period,
                    QuotaUsage.period_key == key,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.count <= 0:
            return
        row.count -= 1
        row.updated_at = _now()
        await db.commit()
        logger.info("配额退还: owner=%s feature=%s", owner, feature)


async def reset(owner: str, feature: str | None = None) -> None:
    """管理员人工重置（清掉计数行）。feature 为空 = 全部功能。"""
    from sqlalchemy import delete

    async with SessionLocal() as db:
        stmt = delete(QuotaUsage).where(QuotaUsage.owner == owner)
        if feature:
            stmt = stmt.where(QuotaUsage.feature == feature)
        await db.execute(stmt)
        await db.commit()
        logger.warning("管理员重置配额: owner=%s feature=%s", owner, feature or "全部")
