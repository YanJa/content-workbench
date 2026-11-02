"""封面品牌名必须跟随排版所选公众号，而非写死默认号「码上聊聊」。

回归背景（2026-11-01 用户反馈）：用别的公众号账号在队列里排版，
生成的封面图右下角品牌名仍是默认主号「码上聊聊」。根因是
`layout.py` 的 `_ensure_cover` 没把 `account_id` 传给封面脚本，
而 `make-cover.py` 的 `BRAND["name"]` 写死为「码上聊聊」。

锁定的行为：
- `_ensure_cover(db, content, theme, account_id)` 应解析该 account_id 对应账号，
  把其 `gzh_name or name` 作为 `brand_name` 透传给 `covers.generate_cover`；
- account_id 留空时回落默认号（gzh_name=码上聊聊），行为不变；
- `make-cover.py` 支持 `--name` 覆盖右下角品牌名。
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  必须导入，create_all 才看得到所有表
from app.db.session import Base
from app.services import layout as layout_mod


@pytest.fixture()
def db_env(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    return Maker


def _make_content(body: str) -> "object":
    from app.models.content import Content

    return Content(
        id=uuid.uuid4().hex,
        owner="default",
        platform="wechat",
        body=body,
        images=[],
        title="回归测试标题",
    )


def _make_account(gzh_name: str, is_default: bool, owner: str = "default") -> "object":
    from app.models.settings import WeChatAccount

    return WeChatAccount(
        id=uuid.uuid4().hex,
        owner=owner,
        name=f"{gzh_name}·运营",
        gzh_name=gzh_name,
        slogan="测试口号",
        is_default=is_default,
    )


async def _seed(Maker, content, accounts):
    async with Maker() as db:
        db.add(content)
        for a in accounts:
            db.add(a)
        await db.commit()
        return [a.id for a in accounts]


class _FakeCoverPath:
    """generate_cover 的假返回值：既当 Path 用，又记录调用参数。"""

    def __init__(self, name: str, **kwargs):
        self._name = name
        self.kwargs = kwargs

    @property
    def name(self):
        return self._name

    def relative_to(self, _base):
        return Path(self._name)

    def read_bytes(self) -> bytes:
        # put_gen 镜像封面字节到对象存储时会读（TOS 接入后 layout.py 调用）
        return b"fake-cover-bytes"


def test_cover_brand_follows_selected_account(db_env):
    Maker = db_env
    content = _make_content("# 我的二号主题文章\n这是副标题段落。")
    default_acc = _make_account("码上聊聊", is_default=True)
    other_acc = _make_account("我的二号", is_default=False)
    asyncio.run(_seed(Maker, content, [default_acc, other_acc]))

    captured = {}

    async def fake_generate_cover(title, out_dir, **kw):
        captured.update(kw)
        return _FakeCoverPath("cover-fake.png", **kw)

    # 让封面通道「可用」，并截获 generate_cover 调用参数
    import app.services.covers as covers_mod

    orig_available = covers_mod.available
    covers_mod.available = lambda: True
    orig_gen = covers_mod.generate_cover
    covers_mod.generate_cover = fake_generate_cover
    try:
        async def _run():
            async with Maker() as db:
                # 选「我的二号」这个非默认号
                await layout_mod._ensure_cover(
                    db, content, theme="", account_id=other_acc.id
                )
        asyncio.run(_run())
    finally:
        covers_mod.available = orig_available
        covers_mod.generate_cover = orig_gen

    assert captured.get("brand_name") == "我的二号", (
        f"封面品牌名应跟随所选账号，实际={captured.get('brand_name')}"
    )


def test_cover_brand_default_when_no_account_id(db_env):
    Maker = db_env
    content = _make_content("# 默认号文章\n副标题段落。")
    default_acc = _make_account("码上聊聊", is_default=True)
    asyncio.run(_seed(Maker, content, [default_acc]))

    captured = {}

    async def fake_generate_cover(title, out_dir, **kw):
        captured.update(kw)
        return _FakeCoverPath("cover-fake.png", **kw)

    import app.services.covers as covers_mod

    orig_available = covers_mod.available
    covers_mod.available = lambda: True
    orig_gen = covers_mod.generate_cover
    covers_mod.generate_cover = fake_generate_cover
    try:
        async def _run():
            async with Maker() as db:
                # 不传 account_id：应回落默认号「码上聊聊」
                await layout_mod._ensure_cover(db, content, theme="", account_id="")
        asyncio.run(_run())
    finally:
        covers_mod.available = orig_available
        covers_mod.generate_cover = orig_gen

    assert captured.get("brand_name") == "码上聊聊", (
        f"未选账号时应回落默认号，实际={captured.get('brand_name')}"
    )


def test_cover_regenerates_when_account_changes(db_env):
    """同一任务换公众号重排（主题不变）也应重生成封面，品牌名跟随新账号。"""
    Maker = db_env
    content = _make_content("# 主题文章\n副标题段落。")
    default_acc = _make_account("码上聊聊", is_default=True)
    other_acc = _make_account("我的二号", is_default=False)
    asyncio.run(_seed(Maker, content, [default_acc, other_acc]))
    # 预置一张旧封面（主题一致、绑定 default 号），用来验证换号会被重建
    old_cover = {
        "id": uuid.uuid4().hex[:12],
        "path": "gen/old-cover.png",  # 不存在也无妨，删除失败被吞
        "url": "",
        "alt": "封面",
        "source": "cover",
        "prompt": "",
        "position": 0,
        "theme": "moyu-green",  # 与本次 theme 相同，仅账号不同
        "account_id": default_acc.id,
    }
    content.images = [old_cover]

    captured = {}

    async def fake_generate_cover(title, out_dir, **kw):
        captured.update(kw)
        return _FakeCoverPath("cover-fake.png", **kw)

    import app.services.covers as covers_mod

    orig_available = covers_mod.available
    covers_mod.available = lambda: True
    orig_gen = covers_mod.generate_cover
    covers_mod.generate_cover = fake_generate_cover
    try:
        async def _run():
            async with Maker() as db:
                await layout_mod._ensure_cover(
                    db, content, theme="moyu-green", account_id=other_acc.id
                )
        asyncio.run(_run())
    finally:
        covers_mod.available = orig_available
        covers_mod.generate_cover = orig_gen

    assert captured, "换账号应触发封面重建（重新调用 generate_cover）"
    assert captured.get("brand_name") == "我的二号", (
        f"重建后的封面品牌名应跟随新账号，实际={captured.get('brand_name')}"
    )


def test_make_cover_name_override_in_html():
    """直接验证 make-cover.py 的 --name 覆盖逻辑（不依赖 Chrome）。"""
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "make-cover.py"
    )
    spec = importlib.util.spec_from_file_location("_cwb_make_cover_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    html = mod.build_html(
        "测试标题",
        "测试副标题",
        tag="",
        date="",
        style="gradient",
        w=900,
        h=383,
        brand={**mod.BRAND, "name": "我的二号"},
    )
    assert "我的二号" in html, "右下角品牌名应被 --name 覆盖"
    assert "码上聊聊" not in html, "不应残留默认品牌名"
