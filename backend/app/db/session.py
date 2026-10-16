"""数据库连接与会话管理（异步 SQLAlchemy）。"""
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 是文件数据库，目录不存在会直接报 unable to open database file。

    这里在建立 engine 前先把目录建好，避免"换个目录启动就崩"的问题。
    """
    if "sqlite" not in url:
        return
    path = url.split("///")[-1]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.DATABASE_URL)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    # SQLite 专用：允许跨线程
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)

SessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


# 轻量列补丁：SQLite 已存在的表不会因模型加字段而自动加列（无 Alembic）。
# 这里在启动时检测缺列并 ALTER TABLE 补上，避免「模型改了但老库没这列」直接 500。
# 只做「加列」这一类安全的非破坏性变更；改类型/删列请写正式迁移脚本。
_SQLITE_COLUMN_PATCHES: dict[str, dict[str, str]] = {
    # 表名 -> {列名: 列定义}
    "contents": {
        "summary": "TEXT DEFAULT ''",
        "body_html": "TEXT DEFAULT ''",
        "theme": "VARCHAR(64)",
        "layouts": "JSON",
        "images": "JSON",
        "layout_status": "VARCHAR(16) DEFAULT 'none'",
        "processing": "VARCHAR(16) DEFAULT ''",
        "updated_at": "INTEGER DEFAULT 0",
    },
    # 关注引导：公众号加「对外号名 / slogan / 二维码」三列。
    # 老库行已存在，非空列必须带 DEFAULT，否则 ALTER TABLE 报错。
    "wechat_accounts": {
        "gzh_name": "VARCHAR(120) DEFAULT ''",
        "slogan": "VARCHAR(200) DEFAULT ''",
        "qrcode_path": "VARCHAR(300) DEFAULT ''",
    },
    # 消息表：推理模型的思维链。老库的消息行补一列空串，回显时不显示折叠块。
    "messages": {
        "reasoning": "TEXT DEFAULT ''",
    },
}


async def _patch_sqlite_columns(conn) -> None:
    """给已存在的表补上模型新增的列（仅 SQLite，仅加列）。"""
    from sqlalchemy import text

    for table, cols in _SQLITE_COLUMN_PATCHES.items():
        exists = (
            await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            )
        ).first()
        if not exists:
            continue  # 表还没建，create_all 会带上新列

        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
        have = {r[1] for r in rows}
        for col, ddl in cols.items():
            if col in have:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


async def init_db() -> None:
    """建表 + 轻量列补丁。生产环境建议改用 Alembic 迁移。"""
    # 必须导入模型包，否则 create_all 看不到任何表
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in settings.DATABASE_URL:
            await _patch_sqlite_columns(conn)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入用。"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
