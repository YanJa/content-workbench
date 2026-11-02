"""同一任务多次推送到公众号草稿箱：应原地替换而非新增一条。

回归点：原先 push_wechat_draft 每次都调 client.add_draft，导致同一队列项
反复推送会在微信草稿箱堆出多条重复草稿。修正后：
  - 队列项已有 wechat_media_id（上一次推送的草稿还在）→ 调 client.update_draft 替换；
  - 原草稿已失效（群发 / 后台删除 / 过期，微信返回 40007）→ 退回 add_draft 新建。

用独立内存 SQLite + fake 微信客户端，不碰开发库、不真打微信接口。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.content import Content, QueueItem
from app.tools import builtin as B


def _make_fake_client():
    """返回能追踪 add/update 调用次数的假微信客户端。"""
    client = AsyncMock()
    client.configured = True
    client.transfer_inline_images = AsyncMock(side_effect=lambda html, images: html)
    client.upload_thumb = AsyncMock(return_value="THUMB")
    client.upload_image = AsyncMock(return_value="https://wx/thumb.jpg")
    client.add_draft = AsyncMock(
        return_value={"ok": True, "media_id": "M_ADD", "message": "已推送到草稿箱"}
    )
    client.update_draft = AsyncMock(
        return_value={
            "ok": True,
            "media_id": "M_UPD",
            "message": "已更新草稿箱里的这篇草稿（原地替换，不新增记录）",
        }
    )
    return client


async def _seed(s, cover_path: str):
    now = int(time.time())
    s.add(
        Content(
            id="c1",
            platform="gzh",
            kind="text",
            title="推了又推",
            body="正文",
            owner="default",
            processing="",
            # 绝对路径，确保 first.exists() 为真、upload_thumb 被触发、封面检查通过
            images=[{"path": cover_path}],
        )
    )
    s.add(
        QueueItem(
            id="q1",
            content_id="c1",
            platform="gzh",
            title="推了又推",
            status="ready",
            owner="default",
            created_at=now,
        )
    )
    await s.commit()


async def _run(tmp_path, monkeypatch):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"x")

    client = _make_fake_client()
    monkeypatch.setattr(
        B, "_resolve_wechat_client",
        AsyncMock(return_value=(client, "test")),
    )
    # 返回 None → 跳过二维码转存分支
    monkeypatch.setattr(B, "_resolve_wechat_account", AsyncMock(return_value=None))
    # 作者名留空
    monkeypatch.setattr(
        "app.routers.settings._get_setting", AsyncMock(return_value="")
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async with Maker() as s:
        await _seed(s, str(cover))

        ctx = {"db": s, "owner": "default"}

        # 第一次推送：队列项还没有 media_id → 必须走 add_draft，不调 update_draft
        res1 = await B.push_wechat_draft(
            title="推了又推", html="<p>正文</p>", content_id="c1", ctx=ctx
        )
        assert res1["ok"] is True, res1
        assert client.add_draft.await_count == 1, "首次推送应调 add_draft"
        assert client.update_draft.await_count == 0, "首次推送不应调 update_draft"
        assert res1.get("replaced") is not True, "首次推送不是替换"

        # 把回填逻辑模拟出来的 media_id 写到队列项（agent 层在真实链路里会写）
        q = (await s.execute(select(QueueItem).where(QueueItem.content_id == "c1"))).scalar_one()
        q.wechat_media_id = "M_UPD"
        await s.commit()

        # 第二次推送：已有 media_id → 必须走 update_draft 替换，add_draft 不再增加
        client.add_draft.await_count = 0  # 重置计数，单独看这一次
        client.update_draft.await_count = 0
        res2 = await B.push_wechat_draft(
            title="推了又推 v2", html="<p>正文 v2</p>", content_id="c1", ctx=ctx
        )
        assert res2["ok"] is True, res2
        assert client.update_draft.await_count == 1, "二次推送应调 update_draft 替换"
        assert client.add_draft.await_count == 0, "二次推送不应再 add 一条"
        assert res2.get("replaced") is True, "二次推送应是替换语义"
        # 回填的 media_id 仍是原值（同一篇草稿被更新）
        assert res2.get("media_id") == "M_UPD", res2

        # 第三次：原草稿已失效（群发/删除，微信返回 40007）→ 退回 add_draft 新建
        client.update_draft.return_value = {
            "ok": False, "errcode": 40007,
            "error": "invalid media_id",
            "hint": "media_id 无效或草稿已不存在",
        }
        client.add_draft.return_value = {
            "ok": True, "media_id": "M_NEW", "message": "已推送到草稿箱"
        }
        client.update_draft.await_count = 0
        client.add_draft.await_count = 0
        res3 = await B.push_wechat_draft(
            title="推了又推 v3", html="<p>正文 v3</p>", content_id="c1", ctx=ctx
        )
        assert res3["ok"] is True, res3
        assert client.update_draft.await_count == 1, "应先尝试 update"
        assert client.add_draft.await_count == 1, "40007 后应 fallback add"
        assert res3.get("replaced") is False, "fallback 新建不是替换"
        assert "原草稿已不存在" in (res3.get("note") or ""), "应提示已新建"

    await engine.dispose()


def test_push_replaces_existing_draft_instead_of_adding(tmp_path, monkeypatch):
    asyncio.run(_run(tmp_path, monkeypatch))
