"""配图说明/位置更新的落库回归测试（2026-10-21 线上 bug 修复）。

bug 背景：update_image 用 `list(c.images)` 浅拷贝后**原地改里面 dict** 的
alt/position，再赋值回 JSON 列。SQLAlchemy 对 JSON 列的变更检测是按序列化
结果比大小——浅拷贝让新旧列表里的 dict 是同一批对象，序列化后完全相同，
commit 静默丢弃：接口返回 `{"ok": true}`，alt/position 一个字节都没落库。

用户侧症状：排版面板里改「图片说明」回车后弹回原值；「设为封面」点了
没反应（position 同样没落库）。

修复：突变后显式 `flag_modified(c, "images")`。这里锁死：
1. alt 更新后**换一个全新会话**能读到（排除同会话缓存造成的假象）；
2. position 更新同理；
3. alt 设为空串是合法更新（`req.alt is not None`，不是 None 就要落库）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到所有表
from app.db.session import Base
from app.models.content import Content
from app.routers.layout import update_image
from app.schemas.models import ImageUpdateRequest

CID = "regress-img-update-001"
IMG_ID = "img001"
OWNER = "default"


def _seed_images():
    return [
        {"id": "cover001", "path": "gen/x/cover.png", "url": "/api/content/images/x/cover.png",
         "alt": "封面", "source": "cover", "prompt": "", "position": 0},
        {"id": IMG_ID, "path": "gen/x/a.png", "url": "/api/content/images/x/a.png",
         "alt": "", "source": "gen", "prompt": "一杯咖啡旁的笔记本电脑", "position": -1},
    ]


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as db:
            db.add(Content(id=CID, platform="gzh", owner=OWNER, title="回归", body="正文",
                           images=_seed_images()))
            await db.commit()

    asyncio.run(_init())
    return Maker


def _read_img(Maker):
    """全新会话读取，排除会话缓存——只有真的落了库才读得到。"""

    async def _run():
        async with Maker() as db:
            c = await db.get(Content, CID)
            return next(i for i in (c.images or []) if i["id"] == IMG_ID)

    return asyncio.run(_run())


def test_alt_update_persists(db_env):
    async def _run():
        async with db_env() as db:
            res = await update_image(
                CID, IMG_ID, ImageUpdateRequest(alt="新的图片说明"),
                db=db, owner=OWNER,
            )
            assert res["ok"] is True

    asyncio.run(_run())
    img = _read_img(db_env)
    assert img["alt"] == "新的图片说明", "alt 必须落库（修复前静默丢弃）"


def test_position_update_persists(db_env):
    async def _run():
        async with db_env() as db:
            await update_image(
                CID, IMG_ID, ImageUpdateRequest(position=0),
                db=db, owner=OWNER,
            )

    asyncio.run(_run())
    img = _read_img(db_env)
    assert img["position"] == 0, "position 必须落库（「设为封面」依赖它）"


def test_alt_clear_to_empty_persists(db_env):
    """先填再清空：空串是合法值（req.alt is not None），不能当成没改。"""
    async def _fill():
        async with db_env() as db:
            await update_image(
                CID, IMG_ID, ImageUpdateRequest(alt="临时说明"),
                db=db, owner=OWNER,
            )

    async def _clear():
        async with db_env() as db:
            await update_image(
                CID, IMG_ID, ImageUpdateRequest(alt=""),
                db=db, owner=OWNER,
            )

    asyncio.run(_fill())
    assert _read_img(db_env)["alt"] == "临时说明"
    asyncio.run(_clear())
    assert _read_img(db_env)["alt"] == "", "清空说明也必须落库"
