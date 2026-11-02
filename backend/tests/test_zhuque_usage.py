"""朱雀用量估算回归测试。

背景（2026-11）：应用内只能看到「本次消耗 X tokens」，没有月度累计——
用户想知道本月还剩多少朱雀额度（EdgeOne Makers 免费 50 万 token/月），
权威值得去控制台看，但应用内给一个「本账号用本平台密钥发起的检测」累计估算
能帮用户心里有数。

这些测试锁死：
  1. 多次检测累加（tokens / calls 都按次累计）；
  2. 查询返回本月 key、累计 tokens、quota、剩余估算、configured、note；
  3. 不同 owner 互不串味；
  4. tokens<=0（配置缺失/失败）不累加。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.zhuque import ZhuqueUsage  # noqa: F401
from app.services import zhuque as zhuque_svc


def _run(coro):
    return asyncio.run(coro)


async def _mk_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/u.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_bump_accumulates_and_query_roundtrip(tmp_path):
    """三次检测累加 tokens/calls；查询回传本月 key、累计、quota、剩余、note。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        month = zhuque_svc._month_key()
        async with Maker() as db:
            zhuque_svc.SessionLocal = Maker  # 让服务函数用同一引擎
            await zhuque_svc.bump_zhuque_usage("o1", 1200)
            await zhuque_svc.bump_zhuque_usage("o1", 880)
            await zhuque_svc.bump_zhuque_usage("o1", 420)

            usage = await zhuque_svc.get_zhuque_month_usage("o1")
            assert usage["month"] == month
            assert usage["tokens"] == 2500
            assert usage["calls"] == 3
            assert usage["quota"] == 500_000
            assert usage["remaining_est"] == 497_500
            assert isinstance(usage["configured"], bool)  # 取决于是否配了 key
            assert "估算" in usage["note"]

            # 新 owner 应从零开始，不串味
            fresh = await zhuque_svc.get_zhuque_month_usage("o2")
            assert fresh["tokens"] == 0
            assert fresh["calls"] == 0
        await engine.dispose()

    _run(go())


def test_negative_or_zero_tokens_ignored(tmp_path):
    """配置缺失/失败返回 0 token：非正消耗不计入累计。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            zhuque_svc.SessionLocal = Maker
            await zhuque_svc.bump_zhuque_usage("o1", 0)   # 失败
            await zhuque_svc.bump_zhuque_usage("o1", -5)  # 异常值兜底
            usage = await zhuque_svc.get_zhuque_month_usage("o1")
            assert usage["tokens"] == 0
            assert usage["calls"] == 0
        await engine.dispose()

    _run(go())
