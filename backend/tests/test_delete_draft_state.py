"""删除微信草稿（delete_wechat_draft）的单元测试。

核心回归：
  1. ``WeChatClient.delete_draft`` 正确调用 draft/delete 并解析 errcode
     （含 40007「media_id 无效/草稿已不存在」的友好提示）；
  2. ``execute_confirmed_tool`` 在删除成功后**必须清掉**队列记录的
     ``wechat_media_id``——不清的后果是微信侧草稿已不在、本地仍显示
     「已推送」，推送按钮消失且无法重新推送；
  3. 删除失败时本地状态原样保留（不误清）；
  4. owner 隔离：只清自己的队列项。

用独立内存 SQLite，不碰开发库；不依赖 pytest-asyncio（直接 asyncio.run）。
"""
from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.content import Content, QueueItem
from app.services import agent as agent_mod
from app.services.wechat import WeChatClient


# ---------------------------------------------------------------- delete_draft


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    """记录 POST 调用的假 httpx client（async with 用法）。"""

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, bytes]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, content: bytes = b"", headers=None):
        import json as _json

        self.calls.append((url, content))
        payload = _json.loads(content.decode("utf-8"))
        # 按 media_id 模拟微信行为：特殊 id 模拟各种错误
        if payload.get("media_id") == "GONE":
            return _FakeResp({"errcode": 40007, "errmsg": "invalid media_id"})
        return _FakeResp({"errcode": 0, "errmsg": "ok"})


def _patch_http(monkey_target: dict) -> None:
    """把 app.services.wechat 里的 httpx.AsyncClient 换成假的。"""
    import app.services.wechat as wx_mod

    monkey_target["orig"] = wx_mod.httpx.AsyncClient
    wx_mod.httpx.AsyncClient = _FakeAsyncClient


def _restore_http(monkey_target: dict) -> None:
    import app.services.wechat as wx_mod

    if "orig" in monkey_target:
        wx_mod.httpx.AsyncClient = monkey_target["orig"]


async def _fake_token() -> str:
    return "FAKE_TOKEN"


async def _run_delete_draft():
    patch: dict = {}
    _patch_http(patch)
    client = WeChatClient(appid="x", secret="y")
    client._access_token = _fake_token  # type: ignore[assignment]
    try:
        # 1) 正常删除：errcode 0 → ok，且 payload 形如 {"media_id": ..., "index": 0}
        res = await client.delete_draft("MEDIA_OK")
        assert res["ok"] is True, res
        assert res["media_id"] == "MEDIA_OK", res
        assert "草稿箱" in res["message"], res

        # 2) 草稿不存在（40007）：友好提示，而不是把 errmsg 裸抛给用户
        res = await client.delete_draft("GONE")
        assert res["ok"] is False, res
        assert res["errcode"] == 40007, res
        assert "media_id" in (res.get("hint") or ""), f"40007 应提示 media_id 无效: {res}"

        # 3) 空 media_id：不发请求，直接报错
        res = await client.delete_draft("")
        assert res["ok"] is False and "media_id" in res["error"], res

        # 4) 未配置凭证：不发请求
        bad = WeChatClient(appid="", secret="")
        res = await bad.delete_draft("MEDIA_OK")
        assert res["ok"] is False and "未配置" in res["error"], res
    finally:
        _restore_http(patch)


# ------------------------------------------------------- execute_confirmed_tool


async def _seed(s) -> None:
    """两条已推送的记录 + 一条别人的。"""
    now = int(time.time())
    rows = [
        ("c_a", "q_a", "MEDIA_A", "default"),
        ("c_b", "q_b", "MEDIA_B", "default"),
        ("c_other", "q_other", "MEDIA_A", "someone_else"),  # 同 media_id，别人owner
    ]
    for cid, qid, media, owner in rows:
        s.add(
            Content(
                id=cid,
                platform="gzh",
                kind="text",
                title=cid,
                body="正文",
                owner=owner,
                processing="",
            )
        )
        s.add(
            QueueItem(
                id=qid,
                content_id=cid,
                platform="gzh",
                title=cid,
                status="ready",
                owner=owner,
                created_at=now,
                wechat_media_id=media,
            )
        )
    await s.commit()


async def _media_map(s) -> dict[str, str | None]:
    rows = (await s.execute(select(QueueItem))).scalars().all()
    return {q.content_id: q.wechat_media_id for q in rows}


def _patch(result: dict):
    async def fake_execute(name, arguments, ctx):
        return result

    original = agent_mod.registry.execute
    agent_mod.registry.execute = fake_execute
    return original


async def _run_state_clearing():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async with Maker() as s:
        await _seed(s)

        # 1) 删除成功（media_id + content_id 都带）→ 命中行清空，别的行不动
        original = _patch({"ok": True, "media_id": "MEDIA_A", "message": "已删除"})
        try:
            res = await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "delete_wechat_draft",
                {"media_id": "MEDIA_A", "content_id": "c_a"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        assert res["ok"] is True
        m = await _media_map(s)
        assert m["c_a"] is None, f"删除成功后必须清掉推送标记: {m}"
        assert m["c_b"] == "MEDIA_B", f"无关行不得被清: {m}"
        assert m["c_other"] == "MEDIA_A", f"别人的队列项不得被清: {m}"

        # 2) 只带 media_id（老调用/异常路径没带 content_id）→ 仍按 media_id 清
        s.add(
            QueueItem(
                id="q_c",
                content_id="c_c",
                platform="gzh",
                title="c_c",
                status="ready",
                owner="default",
                created_at=int(time.time()),
                wechat_media_id="MEDIA_C",
            )
        )
        await s.commit()
        original = _patch({"ok": True, "media_id": "MEDIA_C"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "delete_wechat_draft",
                {"media_id": "MEDIA_C"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_c"] is None, f"只带 media_id 也应清: {m}"

        # 3) 只带 content_id（前端兜底）→ 仍能清
        s.add(
            QueueItem(
                id="q_d",
                content_id="c_d",
                platform="gzh",
                title="c_d",
                status="ready",
                owner="default",
                created_at=int(time.time()),
                wechat_media_id="MEDIA_D",
            )
        )
        await s.commit()
        original = _patch({"ok": True, "media_id": "MEDIA_D"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "delete_wechat_draft",
                {"media_id": "MEDIA_D", "content_id": "c_d"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_d"] is None, f"只带 content_id 也应清: {m}"

        # 4) 删除失败 → 状态原样保留（不能误清，否则用户以为草稿没了）
        original = _patch({"ok": False, "error": "IP 不在白名单"})
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "delete_wechat_draft",
                {"media_id": "MEDIA_B", "content_id": "c_b"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_b"] == "MEDIA_B", f"失败删除不得清状态: {m}"

        # 5) 40007（微信侧草稿已不存在：后台删过/过期清理）→ 也要清。
        # 2026-10-21 实测：4 个旧草稿被外部删除后，本地行全卡在「已推送」
        # 且没有任何出路——用户看不到推送按钮，也没法重新推送。
        original = _patch(
            {"ok": False, "error": "invalid media_id", "errcode": 40007,
             "hint": "media_id 无效或草稿已不存在（可能已被删除）"}
        )
        try:
            await agent_mod.execute_confirmed_tool(
                s,
                {"owner": "default"},
                "delete_wechat_draft",
                {"media_id": "MEDIA_B", "content_id": "c_b"},
            )
        finally:
            agent_mod.registry.execute = original
        await s.commit()
        m = await _media_map(s)
        assert m["c_b"] is None, f"40007（草稿已不存在）必须清掉陈旧标记: {m}"

    await engine.dispose()


def test_delete_draft_client():
    asyncio.run(_run_delete_draft())


def test_delete_draft_clears_push_state():
    asyncio.run(_run_state_clearing())
