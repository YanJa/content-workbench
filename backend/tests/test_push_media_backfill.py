"""推送草稿箱成功后 media_id 回填的单元测试。

核心回归：回填必须按 ``arguments.content_id`` 定位队列项，而不是「最新创建」
的那条。旧实现按 created_at 倒序取第一条，导致：
  - 被推送的记录不变「已推送」→ 用户可二次推送；
  - 无关的最新记录被误标「已推送」→ 真正的推送按钮消失。
用独立内存 SQLite，不碰开发库；不依赖 pytest-asyncio（直接 asyncio.run）。
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.content import Content, QueueItem
from app.services import agent as agent_mod


async def _seed(s) -> None:
    """三条内容 + 队列项：c_old 最早创建，c_new 最新创建。"""
    now = int(time.time())
    for cid, age in (("c_old", 300), ("c_mid", 200), ("c_new", 100)):
        s.add(
            Content(
                id=cid,
                platform="gzh",
                kind="text",
                title=cid,
                body="正文",
                owner="default",
                processing="",
            )
        )
        s.add(
            QueueItem(
                id=f"q_{cid}",
                content_id=cid,
                platform="gzh",
                title=cid,
                status="ready",
                owner="default",
                created_at=now - age,
            )
        )
    await s.commit()


async def _media_map(s) -> dict[str, str | None]:
    rows = (await s.execute(select(QueueItem))).scalars().all()
    return {q.content_id: q.wechat_media_id for q in rows}


def _patch(result: dict):
    async def fake_execute(name, arguments, ctx):
        return result

    original = agent_mod.registry.execute
    agent_mod.registry.execute = fake_execute
    return original


async def _run() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async with Maker() as s:
        await _seed(s)

        # 1) 推送 c_old（不是最新创建的那条）→ 只有 c_old 被回填
        original = _patch({"ok": True, "media_id": "MEDIA_123", "message": "已推送"})
        try:
            res = await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "push_wechat_draft",
                {"title": "c_old", "html": "<p>x</p>", "content_id": "c_old"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        assert res["ok"] is True
        m = await _media_map(s)
        assert m["c_old"] == "MEDIA_123", f"被推送的项应回填 media_id: {m}"
        assert m["c_new"] is None, f"最新创建的项不应被误标: {m}"
        assert m["c_mid"] is None, f"无关项不应被误标: {m}"

        # 2) 兜底：没带 content_id 的老调用仍按最新创建回填
        original = _patch({"ok": True, "media_id": "MEDIA_456"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "push_wechat_draft",
                {"title": "x", "html": "<p>x</p>"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_new"] == "MEDIA_456", f"兜底应回填最新项: {m}"

        # 3) owner 隔离：推送自己的项，不回填别人的队列项
        s.add(
            Content(
                id="c_other",
                platform="gzh",
                kind="text",
                title="别人的",
                body="x",
                owner="someone_else",
                processing="",
            )
        )
        s.add(
            QueueItem(
                id="q_other",
                content_id="c_other",
                platform="gzh",
                title="别人的",
                status="ready",
                owner="someone_else",
                created_at=int(time.time()) + 999,  # 比别人都新，专钓「最新创建」逻辑
            )
        )
        await s.commit()
        original = _patch({"ok": True, "media_id": "MEDIA_789"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "push_wechat_draft",
                {"title": "c_mid", "html": "<p>x</p>", "content_id": "c_mid"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_mid"] == "MEDIA_789", f"自己的项应回填: {m}"
        assert m["c_other"] is None, f"别人的队列项不应被回填: {m}"

        # 4) 推送失败（无 media_id）时不动任何记录
        original = _patch({"ok": False, "error": "IP 不在白名单"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "push_wechat_draft",
                {"title": "c_old", "html": "<p>x</p>", "content_id": "c_old"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_old"] == "MEDIA_123", f"失败推送不得覆盖已有 media_id: {m}"

    await engine.dispose()


def test_push_media_id_backfill_by_content_id():
    asyncio.run(_run())
