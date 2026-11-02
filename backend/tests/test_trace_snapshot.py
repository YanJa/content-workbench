"""验证 AI 痕迹检测快照的落库与回看闭环（meta.aigc_trace）。

覆盖：
- PUT /content/items/{id}/trace-snapshot 把 scan / zhuque 存进 meta.aigc_trace
- 按检测对象（layout / body）各存一份，互不覆盖
- GET 内容时 meta 正确回带，已发布态只读展示用的就是这份
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 建表需要
from app.db.session import Base, get_db
from app.main import app

PASSWORD = "Test@2026"


def _mk_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _override_db(engine, Maker):
    async def _get():
        async with Maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_db] = _get


def _create_all(engine):
    async def _go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    return _go()


def _register(client) -> dict[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"username": f"u{uuid.uuid4().hex[:10]}", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def api():
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    asyncio.run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)
    yield client, headers
    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def test_trace_snapshot_saved_and_read_back(api):
    client, headers = api
    # 建一条内容（入队）
    r = client.post(
        "/api/content/items",
        json={"platform": "gzh", "title": "测试稿", "body": "正文", "sub_type": "article"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    cid = r.json()["content_id"]

    # 存一份「排版产物」检测快照
    scan = {"ok": True, "score": 86, "level_label": "自然", "hits": [{"key": "k", "label": "机械连接词", "severity": "mid", "count": 2}], "note": "口径说明", "source": "layout"}
    zhuque = {"ok": True, "human_ratio": 0.7, "ai_ratio": 0.1, "suspect_ratio": 0.2, "softmax_confidence": 0.95, "source": "layout"}
    r = client.put(
        f"/api/content/items/{cid}/trace-snapshot",
        json={"platform": "gzh", "sub_type": "article", "source": "layout", "scan": scan, "zhuque": zhuque},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # 再存一份「源文」快照，确认不互相覆盖
    scan_body = {"ok": True, "score": 70, "level_label": "略机械", "hits": [], "note": "n", "source": "body"}
    r = client.put(
        f"/api/content/items/{cid}/trace-snapshot",
        json={"source": "body", "scan": scan_body},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # 读回内容，确认 meta.aigc_trace 结构正确
    r = client.get(f"/api/content/items/{cid}", headers=headers)
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    snap = meta["aigc_trace"]
    assert snap["layout"]["scan"]["score"] == 86
    assert snap["layout"]["zhuque"]["ai_ratio"] == 0.1
    assert snap["body"]["scan"]["score"] == 70
    assert snap["saved_at"]
    # 顶层的 platform / sub_type 也在
    assert snap["platform"] == "gzh"
    assert snap["sub_type"] == "article"


def test_trace_snapshot_requires_auth(api):
    client, _ = api
    r = client.put(
        "/api/content/items/whatever/trace-snapshot",
        json={"source": "layout", "scan": {"ok": True, "score": 1}},
    )
    assert r.status_code == 401


def test_trace_snapshot_unknown_content_404(api):
    client, headers = api
    r = client.put(
        "/api/content/items/no-such-id/trace-snapshot",
        json={"source": "layout", "scan": {"ok": True, "score": 1}},
        headers=headers,
    )
    assert r.status_code == 404
