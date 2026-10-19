"""排版链路用量记账回归测试。

背景（2026-10-19 用户对账发现）：layout.py 的 4 处 LLM 调用（pro 主排版 +
回修 + 话题标签 + 小红书规整）**全都没传 usage_sink**，导致 DeepSeek 官方
账单里有、用量页看不见——16/17 号两天差 8 元多（v4-pro 24 次 0 记录）。

这些测试锁死「layout 的每次调用都必须带 sink」：
- 假 LLM client 记录每次 chat() 收到的 usage_sink，断言非 None；
- 并真的触发一次 sink，验证记录能落库（scene=layout、owner、model 正确）。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到所有表
from app.db.session import Base
from app.services import layout as layout_mod


class _FakeLLM:
    """记录 chat() 调用参数，并模拟 llm 层 _emit 的行为（触发 sink）。"""

    def __init__(self, model: str):
        self.model = model
        self.calls: list[dict] = []

    async def chat(self, messages, task="", usage_sink=None, **kwargs):
        self.calls.append({"task": task, "usage_sink": usage_sink})
        if usage_sink is not None:
            # 真 LLM 层在调用结束后会 await sink(payload)；这里照做，
            # 验证「sink 存在且真能落库」而不只是「参数传了」。
            await usage_sink(
                {
                    "model": self.model,
                    "task": task,
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "cached_tokens": 400,
                    "total_tokens": 1500,
                    "latency_ms": 1234,
                }
            )
        return SimpleNamespace(content="<p>回归测试段落</p>")


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    return Maker


def _count_usage(Maker, scene=None):
    from sqlalchemy import text

    async def _run():
        async with Maker() as s:
            if scene:
                rows = (
                    await s.execute(
                        text("SELECT model, owner, scene, prompt_tokens, completion_tokens"
                             " FROM llm_usage WHERE scene = :scene"),
                        {"scene": scene},
                    )
                ).all()
            else:
                rows = (await s.execute(text(
                    "SELECT model, owner, scene, prompt_tokens, completion_tokens FROM llm_usage"
                ))).all()
            return [tuple(r) for r in rows]

    return asyncio.run(_run())


def test_llm_layout_records_usage(db_env, monkeypatch):
    """_llm_layout（v4-pro 主调用 + 回修）必须带 sink 并落库。"""
    fake = _FakeLLM("deepseek-v4-pro")
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    Maker = db_env

    async def _body():
        async with Maker() as db:
            html, errors, warnings = await layout_mod._llm_layout(
                "# 标题\n\n正文内容，足够长以通过校验。",
                layout_mod.DEFAULT_THEME,
                "",
                db=db,
                owner="owner-xyz",
            )
        return html, errors

    html, errors = asyncio.run(_body())

    assert "<p>回归测试段落</p>" in html
    # 每一次 chat 调用都必须带 sink
    assert fake.calls, "fake LLM 未被调用"
    for c in fake.calls:
        assert c["usage_sink"] is not None, f"task={c['task']} 漏传 usage_sink"
        assert c["task"] == "reasoning"
    # 落库：pro 模型、scene=layout、owner 正确
    rows = _count_usage(Maker, scene="layout")
    assert len(rows) == len(fake.calls)
    for model, owner, scene, pt, ct in rows:
        assert model == "deepseek-v4-pro"
        assert owner == "owner-xyz"
        assert scene == "layout"
        assert pt == 1000 and ct == 500


def test_topic_tags_records_usage(db_env, monkeypatch):
    """generate_topic_tags（flash 轻任务）也必须带 sink 并落库。"""
    fake = _FakeLLM("deepseek-flash")
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    Maker = db_env

    async def _body():
        async with Maker() as db:
            return await layout_mod.generate_topic_tags(
                "一篇关于时间管理的文章", db=db, owner="owner-tag"
            )

    asyncio.run(_body())

    assert len(fake.calls) == 1
    assert fake.calls[0]["usage_sink"] is not None
    rows = _count_usage(Maker, scene="layout")
    assert len(rows) == 1
    model, owner, scene, pt, ct = rows[0]
    assert model == "deepseek-flash"
    assert owner == "owner-tag"
    assert scene == "layout"


def test_xiaohongshu_layout_records_usage(db_env, monkeypatch):
    """_layout_xiaohongshu（小红书文本规整）也必须带 sink。"""
    from app.models.content import Content

    fake = _FakeLLM("deepseek-flash")
    monkeypatch.setattr(layout_mod, "get_llm", lambda: fake)
    Maker = db_env

    async def _body():
        async with Maker() as db:
            content = Content(
                id=uuid.uuid4().hex,
                owner="owner-xhs",
                title="小红书笔记",
                body="# 标题\n\n- 要点一\n- 要点二",
                platform="xiaohongshu",
            )
            db.add(content)
            await db.commit()
            return await layout_mod._layout_xiaohongshu(db, content)

    asyncio.run(_body())

    assert len(fake.calls) == 1
    assert fake.calls[0]["usage_sink"] is not None
    rows = _count_usage(Maker, scene="layout")
    assert len(rows) == 1
    assert rows[0][0] == "deepseek-flash"
    assert rows[0][1] == "owner-xhs"
