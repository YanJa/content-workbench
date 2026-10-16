"""发布队列分页的单元测试。

验证分页 offset/limit/total 计算正确，且筛选在分页前生效。
用独立的内存 SQLite 库，不碰开发库；不依赖 pytest-asyncio（直接 asyncio.run）。
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.content import Content, QueueItem


async def _run() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    now = int(time.time())
    async with Maker() as s:
        n = 25
        for i in range(n):
            cid = f"c{i:03d}"
            s.add(
                Content(
                    id=cid,
                    platform="gzh",
                    kind="text",
                    title=f"标题{i}",
                    body="正文",
                    owner="default",
                    processing="",
                )
            )
            s.add(
                QueueItem(
                    id=f"q{i:03d}",
                    content_id=cid,
                    platform="gzh",
                    title=f"标题{i}",
                    status="ready" if i < 10 else "draft",
                    owner="default",
                    created_at=now - i,  # 越大越新
                )
            )
        await s.commit()

        async def page(status: str, page_no: int, size: int):
            conds = [QueueItem.owner == "default"]
            if status != "all":
                conds.append(QueueItem.status == status)
            total = (
                await s.execute(select(func.count()).select_from(QueueItem).where(*conds))
            ).scalar_one()
            stmt = (
                select(QueueItem)
                .where(*conds)
                .order_by(QueueItem.created_at.desc())
                .offset((page_no - 1) * size)
                .limit(size)
            )
            rows = list((await s.execute(stmt)).scalars().all())
            return rows, total

        # 第 1 页 20 条，total=25
        rows1, total1 = await page("all", 1, 20)
        assert total1 == 25, total1
        assert len(rows1) == 20, len(rows1)

        # 第 2 页 5 条
        rows2, total2 = await page("all", 2, 20)
        assert total2 == 25, total2
        assert len(rows2) == 5, len(rows2)

        # 两页无重叠
        ids1 = {r.id for r in rows1}
        ids2 = {r.id for r in rows2}
        assert not (ids1 & ids2), ids1 & ids2

        # 筛选在分页前生效：ready 共 10 条
        rrows, rtotal = await page("ready", 1, 20)
        assert rtotal == 10, rtotal
        assert len(rrows) == 10, len(rrows)

    await engine.dispose()


def test_pagination_offset_and_total():
    asyncio.run(_run())
