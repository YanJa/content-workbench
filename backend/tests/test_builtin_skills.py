"""内置技能包：随代码发布、不落库、用户同名版本优先。

为什么不落库：内置技能是产品的一部分，不是用户数据。落库会带来两难——
升级时被用户改动覆盖回去，或者永远升不了级；还会让技能页多出一堆删不掉、
也停不掉的假行。所以只读文件、按需在内存里注入。
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base
from app.models.skill import Skill
from app.services import skill as skill_svc

PASSWORD = "Test@2026"


def _run(coro):
    return asyncio.run(coro)


def _mk():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_all(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_builtin_zhihu_pack_exists():
    assert "zhihu-content-pack" in skill_svc.builtin_names()


def test_builtin_pack_is_valid_skill():
    """内置技能同样要过 frontmatter 校验 —— 它是靠 parse_skill 读进来的。"""
    s = next(x for x in skill_svc._builtin_skills() if x.name == "zhihu-content-pack")
    assert s.status == "ready"
    assert 0 < len(s.description) <= 1024
    assert "回答" in s.body and "想法" in s.body and "文章" in s.body
    # 官方规范：description 要同时说清「做什么」和「什么时候用」
    assert "知乎" in s.description


def test_builtin_states_no_publish_api():
    """最容易被模型搞错的一条事实：知乎没有发布接口。必须写在技能里。"""
    s = next(x for x in skill_svc._builtin_skills() if x.name == "zhihu-content-pack")
    assert "没有发布接口" in s.body
    assert "不要向用户承诺" in s.body


def test_list_active_includes_builtin_for_empty_owner():
    """新用户什么都没上传，也该能用内置技能。"""
    engine, Maker = _mk()
    _run(_create_all(engine))

    async def go():
        async with Maker() as db:
            rows = await skill_svc.list_active(db, "new-user")
            await db.commit()
            return rows

    rows = _run(go())
    assert any(r.name == "zhihu-content-pack" for r in rows)
    asyncio.run(engine.dispose())


def test_user_version_wins_over_builtin():
    """同名时用户上传的版本优先，且不重复出现两条。"""
    engine, Maker = _mk()
    _run(_create_all(engine))

    async def go():
        async with Maker() as db:
            await skill_svc.upsert_skill(
                db,
                "u1",
                "---\nname: zhihu-content-pack\n"
                "description: 我自己改的知乎包，写知乎回答时用\n---\n我的做法",
                "zhihu-content-pack.md",
            )
            await db.commit()
        async with Maker() as db:
            rows = await skill_svc.list_active(db, "u1")
            await db.commit()
            return rows

    rows = _run(go())
    hits = [r for r in rows if r.name == "zhihu-content-pack"]
    assert len(hits) == 1
    assert hits[0].body == "我的做法"
    asyncio.run(engine.dispose())


def test_activate_builtin_returns_instructions_without_persisting():
    """激活内置技能要能用，但不能因此往库里写一行。"""
    engine, Maker = _mk()
    _run(_create_all(engine))

    async def go():
        async with Maker() as db:
            res = await skill_svc.activate(db, "u1", "zhihu-content-pack")
            await db.commit()
        async with Maker() as db:
            n = len((await db.execute(
                __import__("sqlalchemy").select(Skill)
            )).scalars().all())
            return res, n

    res, row_count = _run(go())
    assert res["ok"] is True
    assert "知乎" in res["instructions"]
    assert row_count == 0  # 内置不落库
    asyncio.run(engine.dispose())


def test_activate_unknown_skill_lists_available():
    engine, Maker = _mk()
    _run(_create_all(engine))

    async def go():
        async with Maker() as db:
            return await skill_svc.activate(db, "u1", "no-such-skill")

    res = _run(go())
    assert res["ok"] is False
    assert "zhihu-content-pack" in res["available"]
    asyncio.run(engine.dispose())
