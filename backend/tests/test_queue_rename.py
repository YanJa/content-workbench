"""队列项标题「未排版前可改」。

入队时标题常常是 AI 随手起的（或者直接取正文第一行），用户想改很正常。
但排版之后标题已经写进 HTML、封面、微信草稿箱，再改就会对不上 —— 所以
规则是**未排版之前可改**，而且这条规则只有一份（routers/queue.py:_title_editable），
列表接口与 PATCH 共用，前端不自己推断。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base
from app.models.content import Content, QueueItem
from app.routers.queue import _title_editable, list_items, update_status
from app.schemas.models import QueueStatusUpdate


def _mk():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


async def _seed(session, *, layout_status="none", processing="", status="draft",
                 media_id=None, title="原标题"):
    s = session
    s.add(Content(id="c1", platform="gzh", kind="text", title=title, body="正文",
                  owner="u1", processing=processing, layout_status=layout_status))
    s.add(QueueItem(id="q1", content_id="c1", platform="gzh", title=title,
                    status=status, owner="u1", wechat_media_id=media_id))
    await s.commit()


# ---------------------------------------------------------------- 1. 判定规则

def test_draft_unlaid_out_is_editable():
    it = QueueItem(id="q", content_id="c", title="t", status="draft")
    assert _title_editable(it, "none", "") is True
    assert _title_editable(it, "", "") is True  # 老数据没有 layout_status 也算没排版


def test_laid_out_is_not_editable():
    it = QueueItem(id="q", content_id="c", title="t", status="ready")
    assert _title_editable(it, "ready", "") is False


def test_pushed_to_draft_box_is_not_editable():
    it = QueueItem(id="q", content_id="c", title="t", status="ready",
                   wechat_media_id="media-1")
    assert _title_editable(it, "none", "") is False  # 没排版，但已经推过草稿箱


def test_processing_is_not_editable():
    it = QueueItem(id="q", content_id="c", title="t", status="draft")
    assert _title_editable(it, "none", "layout") is False


def test_published_or_archived_is_not_editable():
    assert _title_editable(QueueItem(status="published"), "none", "") is False
    assert _title_editable(QueueItem(status="archived"), "none", "") is False


# ---------------------------------------------------------------- 2. PATCH 端点

def test_rename_before_layout_works():
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s)
            out = await update_status("q1", QueueStatusUpdate(title="新标题"), s, "u1")
            assert out.title == "新标题"
            assert out.status == "draft"  # 没传 status 就不动状态
            await s.commit()

            it = await s.get(QueueItem, "q1")
            assert it.title == "新标题"
            # 加工面板显示的是 Content.title —— 必须同步，否则改完看起来没生效
            assert (await s.get(Content, "c1")).title == "新标题"
        await engine.dispose()

    _run(_case())


def test_rename_after_layout_rejected():
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s, layout_status="ready", status="ready")
            with pytest.raises(HTTPException) as ei:
                await update_status("q1", QueueStatusUpdate(title="新标题"), s, "u1")
            assert ei.value.status_code == 409
        await engine.dispose()

    _run(_case())


def test_rename_rejected_after_push_even_without_layout():
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s, media_id="media-1")
            with pytest.raises(HTTPException) as ei:
                await update_status("q1", QueueStatusUpdate(title="新标题"), s, "u1")
            assert ei.value.status_code == 409
        await engine.dispose()

    _run(_case())


def test_empty_title_rejected():
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s)
            with pytest.raises(HTTPException) as ei:
                await update_status("q1", QueueStatusUpdate(title="   "), s, "u1")
            assert ei.value.status_code == 400
        await engine.dispose()

    _run(_case())


def test_too_long_title_rejected():
    """上限必须与前端 maxlength 一致，且不能静默截断（截断 = 用户以为存进去了其实被切）。"""
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s)
            with pytest.raises(HTTPException) as ei:
                await update_status("q1", QueueStatusUpdate(title="长" * 65), s, "u1")
            assert ei.value.status_code == 400

            out = await update_status("q1", QueueStatusUpdate(title="长" * 64), s, "u1")
            assert len(out.title) == 64  # 边界：正好 64 放行
        await engine.dispose()

    _run(_case())


def test_status_only_still_works():
    """回归：只改状态的老调用不能因为加了 title 字段而坏掉。"""
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s)
            out = await update_status("q1", QueueStatusUpdate(status="published"), s, "u1")
            assert out.status == "published"
            assert out.title == "原标题"
            assert out.published_at  # published 要打时间戳
        await engine.dispose()

    _run(_case())


def test_other_users_item_not_found():
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            await _seed(s)
            with pytest.raises(HTTPException) as ei:
                await update_status("q1", QueueStatusUpdate(title="偷改"), s, "someone-else")
            assert ei.value.status_code == 404
        await engine.dispose()

    _run(_case())


# ---------------------------------------------------------------- 3. 列表接口

def test_list_marks_editable_flag():
    """前端的「改」按钮只认这个字段 —— 它必须跟 PATCH 的判定一致。"""
    async def _case():
        engine, Maker = _mk()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            s.add(Content(id="c1", platform="gzh", kind="text", title="未排版", body="b",
                          owner="u1", layout_status="none"))
            s.add(QueueItem(id="q1", content_id="c1", platform="gzh", title="未排版",
                            status="draft", owner="u1"))
            s.add(Content(id="c2", platform="gzh", kind="text", title="已排版", body="b",
                          owner="u1", layout_status="ready"))
            s.add(QueueItem(id="q2", content_id="c2", platform="gzh", title="已排版",
                            status="ready", owner="u1"))
            await s.commit()

            res = await list_items(status="all", platform="all", q="", page=1,
                                   page_size=20, db=s, owner="u1")
            got = {i.id: i for i in res["items"]}
            assert got["q1"].title_editable is True
            assert got["q2"].title_editable is False
            assert got["q2"].layout_status == "ready"
        await engine.dispose()

    _run(_case())
