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


if "sqlite" in settings.DATABASE_URL:
    # WAL + busy_timeout：配额计数是「多请求并发写同一行」，默认的 rollback
    # journal 模式下写锁会直接抛 database is locked，而不是排队等一下。
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001, ANN202
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

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
        # 知乎三形态（v1.5.7）：老行补空串 = 非知乎平台，语义正确。
        "sub_type": "VARCHAR(16) DEFAULT ''",
    },
    "queue_items": {
        "sub_type": "VARCHAR(16) DEFAULT ''",
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
        # 模型调用错误信息：老库消息行补空串，无错误时不显示红色提示条。
        "error": "TEXT DEFAULT ''",
    },
    # 用量表：这次调用烧的是谁的 Key（平台凭证 / 某用户的个人凭证）。
    # 老库行补 'system' —— 改造前的调用全部走 .env，语义正确。
    # 模型池（v1.5.0 建表，v1.5.2 补列）：服务商的默认计费性质。
    # 老库补 'paid' —— 保持加列之前的口径，CF 那一行由播种/迁移改成 'free'。
    "model_providers": {
        "billing_default": "VARCHAR(8) DEFAULT 'paid'",
    },
    "llm_usage": {
        "cred_owner": "VARCHAR(64) DEFAULT 'system'",
        # 这次调用算哪种账（free/paid/unknown）。老库行补空串 = 按 unknown 处理，
        # 也就是维持加列之前的口径（兜底价 + ⚠），不回溯篡改历史账单。
        "billing": "VARCHAR(8) DEFAULT ''",
    },
    # RBAC：用户表加角色与停用标记。老库已有用户行，所以两列都必须带
    # DEFAULT —— 补出来的是 'member' / 1，真正的管理员由启动时自愈
    # ensure_admin_exists() 升级（见 core/rbac.py）。
    "users": {
        "role": "VARCHAR(16) DEFAULT 'member'",
        "is_active": "BOOLEAN DEFAULT 1",
        # ⚠️ 这两列**刻意不带 DEFAULT** —— 语义是「非 NULL 即覆盖角色」，
        # 带了 DEFAULT 就变成每个老用户都被一份空权限覆盖，全站功能锁死。
        # 可空列不需要 DEFAULT，老行自然是 NULL（= 继承角色）。
        "perm_override": "JSON",
        "quota_override": "JSON",
        # 邀请码来源（渠道统计）。可空，老用户 / 代建 / 预置 admin 都是 NULL。
        "invite_code": "VARCHAR(64)",
        # 用户中心资料三件套（2026-10-19）：昵称 / 头像 / 邮箱。
        # 都可空 —— 存量用户没填过，老行自然是 NULL，前端回落用户名。
        # ⚠️ 加模型字段后必须来这里注册一行，否则老库不会补列，
        # 启动即 500（no such column）。无 Alembic，这是唯一的迁移口。
        "nickname": "VARCHAR(64)",
        "avatar": "VARCHAR(256)",
        "email": "VARCHAR(128)",
    },
    # 素材目录（v1.x）：assets 补 folder_id。⚠️ 刻意不带 DEFAULT ——
    # 语义是「NULL = 未归类」，带 DEFAULT 会把老素材误塞进某个目录。
    "assets": {
        "folder_id": "VARCHAR(36)",
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
