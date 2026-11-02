"""排版完成后队列状态推进回归测试。

背景（2026-10-31 用户反馈）：_layout_plaintext（小红书/头条/百家号）与
_layout_zhihu 写完 layout_status='ready' 后，漏了把队列项 status 从 'draft'
推进到 'ready'，导致已排版的任务在列表里仍显示「待加工」。

锁死：这两个分支排版成功后，关联的 QueueItem.status 必须变成 'ready'
（仅当原本是 'draft'；已发布/已放弃的不该被回退）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 让 create_all 看到全部表
from app.db.session import Base
from app.models.content import Content, QueueItem
from app.services import layout as layout_mod


class _FakeLLM:
    async def chat(self, messages, task="", usage_sink=None, **kwargs):
        return SimpleNamespace(content="整理后的纯文本正文")


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}", poolclass=NullPool)
    Maker = async_sessionmaker(engine, expire_on_commit=False)
    # 纯文本规整分支走 get_llm().chat()；给它一个假实现
    monkeypatch.setattr(layout_mod, "get_llm", lambda: _FakeLLM())

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    return engine, Maker


def _seed(Maker, platform, queue_status="draft", sub_type=""):
    async def go():
        async with Maker() as db:
            db.add(
                Content(
                    id="c-1",
                    platform=platform,
                    sub_type=sub_type,
                    body="# 标题\n\n正文段落。",
                    owner="o1",
                )
            )
            db.add(
                QueueItem(
                    id="q-1",
                    content_id="c-1",
                    platform=platform,
                    sub_type=sub_type,
                    title="标题",
                    status=queue_status,
                    owner="o1",
                )
            )
            await db.flush()
            await db.commit()

    asyncio.run(go())


def _run_layout(Maker, platform, sub_type=""):
    async def go():
        async with Maker() as db:
            content = await db.get(Content, "c-1")
            if platform == "zhihu":
                res = await layout_mod._layout_zhihu(db, content)
            else:
                res = await layout_mod._layout_plaintext(db, content, platform)
            await db.commit()
            item = (
                await db.execute(select(QueueItem).where(QueueItem.content_id == "c-1"))
            ).scalars().first()
            return res, item

    return asyncio.run(go())


def test_zhihu_layout_advances_queue_to_ready(db_env):
    _, Maker = db_env
    _seed(Maker, "zhihu", queue_status="draft")
    res, item = _run_layout(Maker, "zhihu")
    assert res["ok"] is True
    assert res["layout_status"] == "ready"
    assert item.status == "ready"


def test_xiaohongshu_layout_advances_queue_to_ready(db_env):
    _, Maker = db_env
    _seed(Maker, "xiaohongshu", queue_status="draft")
    res, item = _run_layout(Maker, "xiaohongshu")
    assert res["ok"] is True
    assert item.status == "ready"


def test_toutiao_layout_advances_queue_to_ready(db_env):
    _, Maker = db_env
    _seed(Maker, "toutiao", queue_status="draft")
    res, item = _run_layout(Maker, "toutiao")
    assert res["ok"] is True
    assert item.status == "ready"


def test_baijiahao_layout_advances_queue_to_ready(db_env):
    _, Maker = db_env
    _seed(Maker, "baijiahao", queue_status="draft")
    res, item = _run_layout(Maker, "baijiahao")
    assert item.status == "ready"


def test_published_queue_not_rolled_back(db_env):
    """已发布的队列项，重新排版后状态不应被回退成待发布。"""
    _, Maker = db_env
    _seed(Maker, "zhihu", queue_status="published")
    res, item = _run_layout(Maker, "zhihu")
    assert res["ok"] is True
    assert res["layout_status"] == "ready"
    assert item.status == "published"
