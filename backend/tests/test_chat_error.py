"""模型调用错误记录的回归测试。

背景（2026-10-23 用户要求「对话时，模型调用返回的错误信息也要记录」）：
SSE 一旦返回 200 就改不了状态码，模型侧报错（凭证失效 / 模型不存在 /
CF 未配置 / 工具轮次超限）只能以 error 事件推给前端。此前错误只活在
前端内存里：刷新或重进会话，这条失败无影无踪，出问题无法回溯。

修复：Message 表加 error 列，chat 流把 error 事件随该轮 assistant
消息落库；build_context 跳过「无正文无工具」的错误占位行，不回灌模型。

这些测试锁死：
  1. 纯失败轮次（一个字没吐出来）也落一行，只带 error；
  2. 流到一半失败（有部分正文）→ 正文与 error 同行落库；
  3. 错误占位行不进模型上下文，正常 assistant 行不受影响；
  4. 历史回显（get_conversation → MessageOut.error）拿得到错误文本；
  5. add_message 的 error 参数原样落库。

约定同 test_revenue_alias.py：无 pytest-asyncio，异步用例写成
`def test_x(): asyncio.run(_run())`，临时 SQLite 引擎。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
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


# ---------------------------------------------------------------- 服务层


def test_add_message_stores_error(tmp_path):
    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            conv = await conv_svc.create_conversation(db, owner="o1")
            m = await conv_svc.add_message(
                db, conv.id, "assistant", "", error="模型调用失败：401 鉴权失败"
            )
            assert m.error == "模型调用失败：401 鉴权失败"
            got = (
                await db.execute(select(Message).where(Message.id == m.id))
            ).scalar_one()
            assert got.error == "模型调用失败：401 鉴权失败"
        await engine.dispose()

    _run(go())


def test_build_context_skips_error_placeholder_row(tmp_path):
    """纯错误占位行（无正文无工具）不进模型上下文；正常行不受影响。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        async with Maker() as db:
            conv = await conv_svc.create_conversation(db, owner="o1")
            await conv_svc.add_message(db, conv.id, "user", "帮我写个标题")
            await conv_svc.add_message(
                db, conv.id, "assistant", "", error="模型调用失败：超时"
            )
            await conv_svc.add_message(db, conv.id, "assistant", "好的，这是标题")
            await db.flush()

            msgs = await conv_svc.build_context(db, conv, "SYSTEM")
            assistant_rows = [m for m in msgs if m["role"] == "assistant"]
            # 只有带正文的那条；空错误行被跳过
            assert len(assistant_rows) == 1
            assert assistant_rows[0]["content"] == "好的，这是标题"
            # 错误文本也不会以任何形式出现在上下文里
            assert all("超时" not in m["content"] for m in msgs)
        await engine.dispose()

    _run(go())


# ---------------------------------------------------------------- 路由层


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


def test_stream_error_only_row_persisted(tmp_path, monkeypatch):
    """一个字没吐出来的失败轮次：也落一行，只带 error，历史回显拿得到。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        _patch_run_chat(
            monkeypatch,
            [{"type": "error", "message": "模型调用失败：Error code: 503 - 过载"}],
        )
        async with Maker() as db:
            req = ChatRequest(message="在吗", use_knowledge=False, use_skills=False)
            resp = await chat_router.chat_stream(
                req, db=db, owner="o1", _perm=_user()
            )
            events = await _drain(resp)
            # 前端照旧收到 error 事件（行为不能变）
            assert any(e["type"] == "error" for e in events)

            msgs = (await db.execute(select(Message))).scalars().all()
            assert len(msgs) == 2  # user + 失败的 assistant 占位行
            row = msgs[1]
            assert row.role == "assistant"
            assert row.content == ""
            assert row.error == "模型调用失败：Error code: 503 - 过载"

            # 历史回显：MessageOut 暴露 error
            detail = await chat_router.get_conversation(
                row.conversation_id, db=db, owner="o1"
            )
            assert detail.messages[1].error == "模型调用失败：Error code: 503 - 过载"
        await engine.dispose()

    _run(go())


def test_stream_partial_text_plus_error_persisted(tmp_path, monkeypatch):
    """流到一半失败：部分正文与 error 落在同一行。"""

    async def go():
        engine, Maker = await _mk_session(tmp_path)
        _patch_run_chat(
            monkeypatch,
            [
                {"type": "delta", "content": "标题一："},
                {"type": "error", "message": "模型调用失败：连接被重置"},
            ],
        )
        async with Maker() as db:
            req = ChatRequest(
                message="写标题", use_knowledge=False, use_skills=False,
                assistant_msg_id="m_fixed_1",
            )
            resp = await chat_router.chat_stream(req, db=db, owner="o1", _perm=_user())
            await _drain(resp)

            row = (
                await db.execute(select(Message).where(Message.id == "m_fixed_1"))
            ).scalar_one()
            assert row.content == "标题一："
            assert row.error == "模型调用失败：连接被重置"
        await engine.dispose()

    _run(go())
