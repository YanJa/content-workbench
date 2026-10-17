"""邀请码：生成、校验、扣减、旧码导入。

这是「自助注册门槛 + 渠道统计锚点」的内核，被两个地方用：

- `routers/auth.py` 的注册端点 —— 校验 + 原子扣减；
- `routers/admin.py` 的管理端点 —— 生成 / 停用 / 列表。

并发安全
--------
扣减走**单语句条件 UPDATE**（`WHERE used < max_uses`），不是「读出来 +1
再写回」—— 后者并发下会超发（两个请求同时读到 used=2、max=3，都以为还能
用一个，实际用了 4 次）。`max_uses IS NULL`（不限次）时条件退化为无条件 +1。

关键口径
--------
- `max_uses` 缺失 / 0 / 负数都当「不限次」，**绝不等于 0 次**（fail-closed
  会把新码一生成就是废码）。
- 用尽 = 即时拒绝，**不改 disabled**：渠道历史要保留，且省一次写。手动停用
  （渠道取消合作）才置 `disabled=True`。
- 校验失败的文案统一模糊（「邀请码无效或已用尽」），不泄露「码存在但用尽」
  这类信息给撞码的人。
"""
from __future__ import annotations

import secrets
import string

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.invite import InviteCode

# 随机码字符集：去掉易混淆的 0/O、1/l/I，抄错率会低很多。
_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1LI")
_DEFAULT_LEN = 10


def generate_code(length: int = _DEFAULT_LEN) -> str:
    """生成一个随机邀请码。用 secrets（加密级随机），不是 random。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _normalize_max(raw: int | None) -> int | None:
    """归一化最大次数。None / 0 / 负数 → None（不限）。"""
    if raw is None or raw <= 0:
        return None
    return raw


async def create(
    *,
    code: str | None = None,
    note: str = "",
    max_uses: int | None = None,
    created_by: str | None = None,
    db=None,
) -> InviteCode:
    """创建一条邀请码。

    `code` 留空则随机生成；撞唯一约束就换一个重试（概率极低，但别让它 500）。
    传入 `db`（业务事务里复用会话）或省略（独立会话）。
    """
    own_session = db is None
    session = db or SessionLocal()
    try:
        for _ in range(5):
            final_code = (code or "").strip() or generate_code()
            inv = InviteCode(
                code=final_code,
                note=(note or "").strip(),
                max_uses=_normalize_max(max_uses),
                used=0,
                disabled=False,
                created_by=created_by,
            )
            session.add(inv)
            try:
                await session.flush()
                if own_session:
                    await session.commit()
                else:
                    await session.flush()
                return inv
            except IntegrityError:
                await session.rollback()
                if code:  # 用户指定了码且已存在 → 直接报，别再随机糊弄
                    raise HTTPException(409, "邀请码已存在")
                continue  # 随机撞了唯一约束，换一个
        raise HTTPException(500, "生成邀请码失败，请重试")
    finally:
        if own_session:
            await session.close()


async def consume(code: str) -> InviteCode:
    """校验并原子扣减一次用量。

    成功返回该 InviteCode（此时 used 已 +1 并提交）；失败抛 403。
    独立会话，立即提交 —— 不搭注册事务的车，避免「注册后续失败回滚」把
    扣减也撤了，或「注册还没提交」时并发又放进来一个。
    """
    code = (code or "").strip()
    if not code:
        raise HTTPException(403, "邀请码无效或已用尽")

    async with SessionLocal() as db:
        stmt = (
            update(InviteCode)
            .where(InviteCode.code == code, InviteCode.disabled.is_(False))
            .values(used=InviteCode.used + 1)
        )
        # 不限次：max_uses IS NULL 时不要加 used < max_uses 条件（NULL 比较恒假）
        stmt = stmt.where(InviteCode.max_uses.is_(None) | (InviteCode.used < InviteCode.max_uses))

        res = await db.execute(stmt)
        await db.commit()
        if res.rowcount == 1:
            row = (
                await db.execute(select(InviteCode).where(InviteCode.code == code))
            ).scalar_one()
            return row

    # 没扣成：码不存在 / 已停用 / 已用尽
    raise HTTPException(403, "邀请码无效或已用尽")


async def resolve(code: str | None) -> InviteCode | None:
    """按码查记录（只读）。给管理页 / 用户列表反查备注用。"""
    if not code:
        return None
    async with SessionLocal() as db:
        return (
            await db.execute(select(InviteCode).where(InviteCode.code == code))
        ).scalar_one_or_none()


async def import_legacy_code() -> None:
    """启动时把 .env 里的旧全局邀请码导入为一条「不限次」的记录。

    迁移语义：老码 `INVITE_CODE` 此前是「一个码走天下」且不限次，导入后保持
    等价（max_uses=NULL、备注「旧全局码」），管理员能在管理页看到并停用它。
    已注册用户的历史来源无法回溯（旧表没记），这是可接受的——迁移只保证
    「老码继续能用」，不伪造历史归属。

    幂等：码已存在就跳过。未配置旧码 / 已迁移过都不报错。
    """
    legacy = (settings.INVITE_CODE or "").strip()
    if not legacy:
        return
    async with SessionLocal() as db:
        exists = (
            await db.execute(select(InviteCode.id).where(InviteCode.code == legacy))
        ).first()
        if exists:
            return
        db.add(
            InviteCode(
                code=legacy,
                note="旧全局码（迁移）",
                max_uses=None,
                used=0,
                disabled=False,
                created_by=None,
            )
        )
        await db.commit()
        logger.info("已导入旧全局邀请码 %r 为新体系记录", legacy)
