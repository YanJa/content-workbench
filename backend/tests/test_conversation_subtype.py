"""会话「平台-子形态」持久化回归测试。

背景（2026-11 修复）：用户选了「今日头条-图文」之类的平台与子形态后，切走再
切回会话，顶部选择器会回落成「通用对话」—— 因为子形态从未落库，前端打开会话
时也只回填了消息、没恢复选择器。

修复：每次发送都把 `scene` / `sub_type` 写回 Conversation（sub_type 是新增列），
`get_conversation` 随详情回传，前端据此恢复顶部选择器。

这些测试锁死：
  1. 隐式建会话时，首条发送把 platform/sub_type 写回会话；
  2. 已有会话切换平台再发，session 的 scene/sub_type 跟随更新；
  3. get_conversation 把 sub_type 透传给前端（ConversationOut.sub_type）；
  4. 无子形态平台（如通用对话 / 小红书）sub_type 归空串、scene 回落 chat。

约定同 test_chat_error.py：无 pytest-asyncio，异步用例写成
`def test_x(): asyncio.run(_run())`，临时 SQLite 引擎。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.conversation import Conversation, Message  # noqa: F401
from app.routers import chat as chat_router
from app.schemas.models import ChatRequest
from app.services import conversation as conv_svc


def _run(coro):
    return asyncio.run(coro)


async def _mk_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _user():
    return SimpleNamespace(id="u1")


async def _drain(resp) -> list[dict]:
    """消费 SSE 响应体，解析出事件 dict。"""
    events = []
    async for chunk in resp.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _patch_run_chat(monkeypatch, events: list[dict]):
    async def fake(db, ctx, messages, task="copywriting"):
        for e in events:
            yield e

    monkeypatch.setattr(chat_router, "run_chat", fake)


def test_implicit_create_stores_scene_and_subtype(tmp_path, monkeypatch):
    """隐式建会话（首条发送）：平台与子形态写回会话行。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        _patch_run_chat(monkeypatch, [{"type": "delta", "content": "正文"}])
        async with Maker() as db:
            req = ChatRequest(
                message="写一条微头条",
                platform="toutiao",
                sub_type="weitoutiao",
                use_knowledge=False,
                use_skills=False,
            )
            resp = await chat_router.chat_stream(req, db=db, owner="o1", _perm=_user())
            events = await _drain(resp)
            cid = next(e["conversation_id"] for e in events if e["type"] == "conversation")

            conv = (
                await db.execute(select(Conversation).where(Conversation.id == cid))
            ).scalar_one()
            assert conv.scene == "toutiao"
            assert conv.sub_type == "weitoutiao"

            # 详情接口把 sub_type 透传出去，前端才能恢复选择器
            detail = await chat_router.get_conversation(cid, db=db, owner="o1")
            assert detail.scene == "toutiao"
            assert detail.sub_type == "weitoutiao"
        await engine.dispose()

    _run(go())


def test_existing_conversation_updates_scene_and_subtype(tmp_path, monkeypatch):
    """已有会话：换平台再发，scene/sub_type 跟随更新（最后使用为准）。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        _patch_run_chat(monkeypatch, [{"type": "delta", "content": "x"}])
        async with Maker() as db:
            # 先以「通用对话」建一条
            conv = await conv_svc.create_conversation(db, owner="o1")
            await db.commit()

            # 再发一条：切到知乎-文章
            req = ChatRequest(
                conversation_id=conv.id,
                message="写一篇知乎专栏",
                platform="zhihu",
                sub_type="article",
                use_knowledge=False,
                use_skills=False,
            )
            resp = await chat_router.chat_stream(req, db=db, owner="o1", _perm=_user())
            await _drain(resp)

            refreshed = (
                await db.execute(select(Conversation).where(Conversation.id == conv.id))
            ).scalar_one()
            assert refreshed.scene == "zhihu"
            assert refreshed.sub_type == "article"
        await engine.dispose()

    _run(go())


def test_no_subtype_platform_normalizes_to_empty(tmp_path, monkeypatch):
    """无子形态平台（小红书）或通用对话：sub_type 归空串、scene 回落 chat。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        _patch_run_chat(monkeypatch, [{"type": "delta", "content": "x"}])

        async def stream_and_read(platform, sub_type):
            async with Maker() as db:
                req = ChatRequest(
                    message="hi",
                    platform=platform,
                    sub_type=sub_type,
                    use_knowledge=False,
                    use_skills=False,
                )
                resp = await chat_router.chat_stream(req, db=db, owner="o1", _perm=_user())
                events = await _drain(resp)
                cid = next(e["conversation_id"] for e in events if e["type"] == "conversation")
                conv = (
                    await db.execute(select(Conversation).where(Conversation.id == cid))
                ).scalar_one()
                return conv.scene, conv.sub_type

        # 小红书本身没有子形态概念：前端多带一个字段不该炸，落库归空串
        scene, sub = await stream_and_read("xiaohongshu", "article")
        assert scene == "xiaohongshu"
        assert sub == ""

        # 通用对话：platform=None
        scene, sub = await stream_and_read(None, "")
        assert scene == "chat"
        assert sub == ""
        await engine.dispose()

    _run(go())
