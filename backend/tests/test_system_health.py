"""「服务健康」详细数据（/api/system/health-detail）与错误环形缓冲。

errorlog 回答「最近出了什么岔子」：进程内 deque、新的在前、超长丢最旧的。
health-detail 是设置页服务健康卡片的唯一数据源：运行时 / 外部通道 / 模型池 /
最近异常四个块，只读、不拨号、登录即可看。
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.core import errorlog
from app.db.session import Base, get_db
from app.main import app

PASSWORD = "Test@2026"


# ---------------------------------------------------------------- errorlog


def test_errorlog_recent_is_newest_first():
    errorlog.clear()
    errorlog.record("llm", "第一条", ts=100)
    errorlog.record("llm", "第二条", ts=200)
    items = errorlog.recent()
    assert [i["message"] for i in items] == ["第二条", "第一条"]


def test_errorlog_drops_oldest_beyond_capacity():
    errorlog.clear()
    for i in range(60):
        errorlog.record("http", f"e{i}", ts=i)
    items = errorlog.recent(limit=100)
    assert len(items) == 50  # 环形缓冲上限
    assert items[0]["message"] == "e59"  # 最新的在前
    assert "e0" not in [i["message"] for i in items]  # 最旧的被挤掉


def test_errorlog_truncates_long_fields():
    errorlog.clear()
    errorlog.record("llm", "长" * 1000, detail="细" * 1000)
    item = errorlog.recent()[0]
    assert len(item["message"]) <= 500 and len(item["detail"]) <= 500


def test_http_record_shapes_message():
    errorlog.clear()
    errorlog.record_http("/api/x", "POST", RuntimeError("炸了"))
    item = errorlog.recent()[0]
    assert item["source"] == "http"
    assert "POST /api/x" in item["message"] and "炸了" in item["message"]


# ---------------------------------------------------------------- 接口


def _mk_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _override_db(engine, Maker):
    async def _get():
        async with Maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get
    return engine


def test_health_detail_requires_login():
    errorlog.clear()
    engine, Maker = _mk_db()
    _override_db(engine, Maker)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    client = TestClient(app)
    assert client.get("/api/system/health-detail").status_code == 401
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def test_health_detail_shape_for_logged_in_user():
    errorlog.clear()
    errorlog.record("llm", "历史异常一条", ts=12345)
    engine, Maker = _mk_db()
    _override_db(engine, Maker)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    client = TestClient(app)
    # 首个注册用户永远放行（无需邀请码）
    r = client.post(
        "/api/auth/register", json={"username": "hc1", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    resp = client.get("/api/system/health-detail", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 运行时块
    rt = body["runtime"]
    for key in ("version", "env", "started_at", "uptime_secs", "uptime"):
        assert key in rt, f"runtime 缺 {key}"
    assert rt["version"]

    # 外部通道块：三行状态灯，都是 ok/detail 结构
    for name in ("wechat", "zhihu", "cloudflare"):
        assert name in body["channels"], f"channels 缺 {name}"
        assert isinstance(body["channels"][name]["ok"], bool)
        assert body["channels"][name]["detail"]

    # 模型池块：8 个功能槽一个不少（没绑定的槽 primary=None）
    feats = {b["feature"] for b in body["pool"]["bindings"]}
    assert feats == {"chat", "summary", "layout", "layout_tags",
                     "xhs_format", "image_prompt_translate", "image", "embedding"}
    assert isinstance(body["pool"]["alerts"], list)

    # 最近异常：新的在前
    errs = body["recent_errors"]
    assert errs and errs[0]["message"] == "历史异常一条"
    assert errs[0]["source"] == "llm"

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def test_health_detail_db_size_is_none_for_memory_db():
    """内存库（测试）没有磁盘文件 → db_size_bytes 应为 None 而不是报错。"""
    errorlog.clear()
    engine, Maker = _mk_db()
    _override_db(engine, Maker)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    client = TestClient(app)
    token = client.post(
        "/api/auth/register", json={"username": "hc2", "password": PASSWORD}
    ).json()["token"]
    body = client.get(
        "/api/system/health-detail", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert body["runtime"]["db_size_bytes"] is None
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())
