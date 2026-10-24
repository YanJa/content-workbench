"""知乎选题闭环：推荐可答问题 + 同问题头部回答。

这两个端点 2026-10-24 实测确认的形态（官方文档写错了，别照抄文档）：

    推荐问题  GET /api/v1/user/question_recommendations?Count=N   ← /user/ 不是 /content/
    问题回答  GET /api/v1/content/question_answers?QuestionUrl=<完整URL>&Limit=N

第二条只认**完整问题 URL**，传纯 id 会被平台拒（"QuestionUrl must be a valid
Zhihu question URL"）—— 所以必须先 extract_question_id 再拼回去。

价值在哪：知乎回答最忌讳复述高赞已经说透的东西，但模型并不知道高赞写了什么。
把头部回答摘要当「已有答案边界」喂进去，「必须写出增量」才有着落。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base, get_db
from app.main import app
from app.services import zhihu

PASSWORD = "Test@2026"
QID = "2094350166518249285"
QURL = f"https://www.zhihu.com/question/{QID}"


def _run(coro):
    return asyncio.run(coro)


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


def _register(client) -> dict[str, str]:
    import uuid

    r = client.post(
        "/api/auth/register",
        json={"username": f"u{uuid.uuid4().hex[:10]}", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _create_all(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------- 服务层

def test_question_answers_rejects_non_question_input():
    res = _run(zhihu.question_answers("不是链接"))
    assert res["ok"] is False
    assert res["items"] == []


def test_question_answers_sends_full_url(monkeypatch):
    """必须拼成完整 URL 再发 —— 传 id 平台会拒。"""
    seen: dict = {}

    async def fake_get(path: str, params: dict):
        seen["path"] = path
        seen["params"] = params
        return {"ok": True, "data": {"Items": [{"Summary": "已有回答一句", "Url": QURL}]}}

    monkeypatch.setattr(zhihu, "_configured", lambda: True)
    monkeypatch.setattr(zhihu, "_get_json", fake_get)
    monkeypatch.setattr(zhihu, "_bump", lambda n=1: asyncio.sleep(0))

    res = _run(zhihu.question_answers(f"https://www.zhihu.com/question/{QID}?utm_source=hot", 3))
    assert res["ok"] is True
    assert seen["path"] == "/api/v1/content/question_answers"
    # utm 必须剥掉，只留干净的问题页 URL
    assert seen["params"]["QuestionUrl"] == QURL
    assert seen["params"]["Limit"] == 3
    assert res["items"][0]["summary"] == "已有回答一句"


def test_long_summary_is_truncated(monkeypatch):
    """摘要是给模型划边界用的，不是给它当素材抄的 —— 200 字封顶。"""
    async def fake_get(path: str, params: dict):
        return {"ok": True, "data": {"Items": [{"Summary": "长" * 500, "Url": QURL}]}}

    monkeypatch.setattr(zhihu, "_configured", lambda: True)
    monkeypatch.setattr(zhihu, "_get_json", fake_get)
    monkeypatch.setattr(zhihu, "_bump", lambda n=1: asyncio.sleep(0))

    res = _run(zhihu.question_answers(QURL))
    assert len(res["items"][0]["summary"]) <= 201


def test_recommendations_maps_fields(monkeypatch):
    async def fake_get(path: str, params: dict):
        assert path == "/api/v1/user/question_recommendations"
        return {
            "ok": True,
            "data": {"Items": [{"Title": "怎么选第一份工作", "Url": f"{QURL}?utm_source=x"}]},
        }

    monkeypatch.setattr(zhihu, "_configured", lambda: True)
    monkeypatch.setattr(zhihu, "_get_json", fake_get)
    monkeypatch.setattr(zhihu, "_bump", lambda n=1: asyncio.sleep(0))

    res = _run(zhihu.question_recommendations(5))
    assert res["ok"] is True
    assert res["items"][0]["title"] == "怎么选第一份工作"
    assert res["items"][0]["url"] == QURL  # utm 已剥
    assert res["items"][0]["question_id"] == QID


def test_unconfigured_returns_clean_error(monkeypatch):
    monkeypatch.setattr(zhihu, "_configured", lambda: False)
    res = _run(zhihu.question_recommendations())
    assert res["ok"] is False
    assert "ZHIHU_ACCESS_SECRET" in res["error"]


# ---------------------------------------------------------------- prompt 注入

def test_answers_as_prompt_is_empty_without_items():
    assert zhihu.answers_as_prompt([]) == ""


def test_answers_as_prompt_sets_boundary_not_material():
    """文案必须说清「避开」而不是「参考」—— 说参考模型就会去复述。"""
    p = zhihu.answers_as_prompt([{"summary": "已有的观点", "url": QURL}])
    assert "已有的观点" in p
    assert "边界" in p
    assert "严禁复述" in p


# ---------------------------------------------------------------- 路由

def test_endpoints_require_login():
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    _run(_create_all(engine))
    client = TestClient(app)
    assert client.get("/api/zhihu/question-recommendations").status_code == 401
    assert client.get(f"/api/zhihu/question-answers?url={QURL}").status_code == 401
    app.dependency_overrides.clear()
    _run(engine.dispose())


def test_question_answers_requires_url():
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    _run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)
    r = client.get("/api/zhihu/question-answers", headers=headers)
    assert r.status_code == 400
    app.dependency_overrides.clear()
    _run(engine.dispose())


def test_quota_exhausted_is_reported_not_silent(monkeypatch):
    """额度到了要明确说，不能静默返回空列表 —— 用户会以为是接口坏了。"""
    engine, Maker = _mk_db()
    _override_db(engine, Maker)
    _run(_create_all(engine))
    client = TestClient(app)
    headers = _register(client)

    async def fake_usage():
        return {"used": 100, "quota": 100, "safe_to_fetch": False}

    monkeypatch.setattr(zhihu, "usage_today", fake_usage)
    r = client.get("/api/zhihu/question-recommendations", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "额度" in body["error"]
    app.dependency_overrides.clear()
    _run(engine.dispose())
