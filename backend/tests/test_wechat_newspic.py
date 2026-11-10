"""微信公众号草稿箱 news（图文）/ newspic（图片消息·贴图）双类型回归测试。

依据 2026-11 微信官方 draft/add、draft/update 文档：
  - draft/add   的 articles 是 **数组**；draft/update 的 articles 是 **对象**
  - newspic：content 只支持纯文本，image_info.image_list 业务上限 6 张（接口 20 张）且必须是
    永久 MediaID，首图即封面，不传 thumb_media_id
  - news：content 是 HTML，thumb_media_id 必填（封面）

全部用 fake httpx / fake client + 内存 SQLite，不真打微信接口、不推真实草稿。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.content import Content, QueueItem
from app.services import wechat as W
from app.tools import builtin as B


# ---------------------------------------------------------------- 贴图文案拆分
def test_split_newspic_text_plain_first_line():
    """排版产物第一行就是首图标题（_layout_gzh_tietu 的 prompt 不输出 # 符号）"""
    text = "月薪五千该不该买这台机器\n\n上周我蹲了三天直播间。\n\n- 💡 先算回本周期\n- 💡 别看赠品"
    title, body = W.split_newspic_text(text)
    assert title == "月薪五千该不该买这台机器"
    assert body.startswith("上周我蹲了三天直播间")
    assert "💡 先算回本周期" in body
    assert title not in body


def test_split_newspic_text_legacy_markdown():
    """历史数据里的 '# 标题' / 加粗 / 列表 / 引用 / 链接都要清成纯文本"""
    text = "# 首图标题\n\n**重点**在这里\n\n- 💡 第一点\n> 引用\n\n[官网](https://example.com)"
    title, body = W.split_newspic_text(text, "兜底标题")
    assert title == "首图标题"
    assert "**" not in body and "`" not in body
    assert "重点在这里" in body
    assert "官网" in body and "https://" not in body
    assert body.startswith("重点在这里")


def test_split_newspic_text_fallback_and_title_limit():
    assert W.split_newspic_text("   \n\n ", "内容标题") == ("内容标题", "")
    title, body = W.split_newspic_text("字" * 60 + "\n\n正文")
    assert len(title) == 32, "微信 title 上限 32 字"
    assert body == "正文"


def test_clean_newspic_text_keeps_emoji_collapses_blank_lines():
    out = W.clean_newspic_text("A\n\n\n\n\nB\n- 💡 x\n`code`")
    assert "\n\n\n" not in out
    assert "💡" in out
    assert "code" in out and "`" not in out


# ---------------------------------------------------------------- 载荷构造
class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _CaptureClient:
    """替换 httpx.AsyncClient，捕获 POST 的 body，不发真实请求。"""

    bodies: list[dict] = []
    urls: list[str] = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, content=None, headers=None, **kwargs):
        _CaptureClient.urls.append(url)
        _CaptureClient.bodies.append(json.loads(content.decode("utf-8")))
        return _FakeResp({"media_id": "M_NEW", "errcode": 0})


def _client_with_fake_http(monkeypatch):
    _CaptureClient.bodies = []
    _CaptureClient.urls = []
    monkeypatch.setattr(W.httpx, "AsyncClient", _CaptureClient)
    c = W.WeChatClient(appid="x", secret="y")
    c._access_token = AsyncMock(return_value="TOKEN")
    return c


def test_add_draft_news_payload_unchanged(monkeypatch):
    """图文消息载荷保持原样，不因新增 newspic 支持而漂移"""

    async def _run():
        c = _client_with_fake_http(monkeypatch)
        r = await c.add_draft("标题", "<p>正文</p>", digest="摘要",
                              thumb_media_id="THUMB", author="作者")
        assert r["ok"] is True, r
        art = _CaptureClient.bodies[-1]["articles"][0]
        assert art["thumb_media_id"] == "THUMB"
        assert art["content"] == "<p>正文</p>"
        assert art["digest"] == "摘要" and art["author"] == "作者"
        assert "image_info" not in art
        assert "article_type" not in art, "news 不显式传 article_type，沿用老载荷"

    asyncio.run(_run())


def test_add_draft_newspic_payload(monkeypatch):
    async def _run():
        c = _client_with_fake_http(monkeypatch)
        r = await c.add_draft("首图标题", "纯文本正文", article_type="newspic",
                              image_media_ids=["MID1", "MID2"], thumb_media_id="")
        assert r["ok"] is True, r
        art = _CaptureClient.bodies[-1]["articles"][0]
        assert art["article_type"] == "newspic"
        assert art["content"] == "纯文本正文"
        assert art["image_info"]["image_list"] == [
            {"image_media_id": "MID1"}, {"image_media_id": "MID2"}
        ]
        # 图片消息首图即封面：封面/摘要/阅读原文都是 news 专属，不该出现
        assert "thumb_media_id" not in art
        assert "digest" not in art
        assert "content_source_url" not in art

    asyncio.run(_run())


def test_add_draft_newspic_requires_image(monkeypatch):
    async def _run():
        c = _client_with_fake_http(monkeypatch)
        r = await c.add_draft("标题", "正文", article_type="newspic")
        assert r["ok"] is False and "至少" in r["error"], r
        assert _CaptureClient.bodies == [], "缺图时不该发请求"
        # news 无封面仍然硬拦（老行为不回归）
        r2 = await c.add_draft("标题", "<p>x</p>")
        assert r2["ok"] is False and "封面" in r2["error"], r2

    asyncio.run(_run())


def test_add_draft_newspic_caps_6_images(monkeypatch):
    """产品口径 5-6 张：载荷最多带 6 张（微信接口本身允许 20 张）"""

    async def _run():
        c = _client_with_fake_http(monkeypatch)
        await c.add_draft("标题", "正文", article_type="newspic",
                          image_media_ids=[f"M{i}" for i in range(25)])
        lst = _CaptureClient.bodies[-1]["articles"][0]["image_info"]["image_list"]
        assert len(lst) == 6 and lst[0] == {"image_media_id": "M0"}
        assert lst[-1] == {"image_media_id": "M5"}

    asyncio.run(_run())


def test_update_draft_uses_object_articles(monkeypatch):
    """官方 draft/update 的 articles 是 object（旧实现用数组），news/newspic 都适用"""

    async def _run():
        c = _client_with_fake_http(monkeypatch)
        r = await c.update_draft("MEDIA", "标题", "<p>正文</p>", thumb_media_id="THUMB")
        assert r["ok"] is True, r
        p = _CaptureClient.bodies[-1]
        assert p["media_id"] == "MEDIA" and p["index"] == 0
        assert isinstance(p["articles"], dict), "update 的 articles 必须是 object"
        assert p["articles"]["thumb_media_id"] == "THUMB"

        await c.update_draft("MEDIA", "首图", "纯文本", article_type="newspic",
                             image_media_ids=["MID1"])
        a = _CaptureClient.bodies[-1]["articles"]
        assert isinstance(a, dict)
        assert a["article_type"] == "newspic"
        assert a["image_info"]["image_list"] == [{"image_media_id": "MID1"}]
        assert "thumb_media_id" not in a

    asyncio.run(_run())


# ---------------------------------------------------------------- push 工具端到端
def _fake_client():
    client = AsyncMock()
    client.configured = True
    client.upload_thumb = AsyncMock(side_effect=lambda p: f"MID_{Path(p).name}")
    client.transfer_inline_images = AsyncMock(side_effect=lambda h, i: h)
    client.upload_image = AsyncMock(return_value="https://wx/x.jpg")
    client.add_draft = AsyncMock(
        return_value={"ok": True, "media_id": "M_ADD", "message": "已推送到草稿箱"}
    )
    client.update_draft = AsyncMock(
        return_value={"ok": True, "media_id": "M_UPD", "message": "已更新草稿"}
    )
    return client


async def _seed(s, paths, *, media_id=None):
    now = int(time.time())
    s.add(
        Content(
            id="c1", platform="gzh", sub_type="tietu", kind="text",
            title="内容标题（不是首图标题）", body="原文", owner="default",
            processing="", images=[{"path": p} for p in paths],
            layouts={"gzh": {"text": "# 库里的首图标题\n\n库里正文"}},
        )
    )
    s.add(
        QueueItem(
            id="q1", content_id="c1", platform="gzh", sub_type="tietu",
            title="内容标题", status="ready", owner="default",
            created_at=now, wechat_media_id=media_id,
        )
    )
    await s.commit()


async def _maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _patch_common(monkeypatch, client):
    monkeypatch.setattr(
        B, "_resolve_wechat_client", AsyncMock(return_value=(client, "test"))
    )
    monkeypatch.setattr("app.routers.settings._get_setting", AsyncMock(return_value=""))


def test_push_newspic_end_to_end(tmp_path, monkeypatch):
    async def _run():
        p1, p2 = tmp_path / "a.png", tmp_path / "b.jpg"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, [str(p1), str(p2)])
            res = await B.push_wechat_draft(
                title="内容标题（不是首图标题）",
                article_type="newspic",
                text="# 首图标题\n\n正文第一句\n\n💡 第一点",
                content_id="c1", ctx={"db": s, "owner": "default"},
            )
            assert res["ok"] is True, res
            assert res["article_type"] == "newspic" and res["image_count"] == 2
            # 贴图不做正文 HTML 转存（newspic 的 content 是纯文本）
            assert client.transfer_inline_images.await_count == 0
            assert client.upload_thumb.await_count == 2
            assert client.add_draft.await_count == 1
            kw = client.add_draft.await_args.kwargs
            assert kw["article_type"] == "newspic"
            assert kw["title"] == "首图标题", "标题取文案首行，不是内容标题"
            assert kw["html"] == "正文第一句\n\n💡 第一点"
            assert kw["image_media_ids"] == ["MID_a.png", "MID_b.jpg"]
            assert not kw.get("thumb_media_id")
        await engine.dispose()

    asyncio.run(_run())


def test_push_newspic_falls_back_to_layouts_text(tmp_path, monkeypatch):
    """前端没传 text 时，回落排版产物 layouts.gzh.text"""

    async def _run():
        p = tmp_path / "a.png"
        p.write_bytes(b"x")
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, [str(p)])
            res = await B.push_wechat_draft(
                title="兜底标题", article_type="newspic", content_id="c1",
                ctx={"db": s, "owner": "default"},
            )
            assert res["ok"] is True, res
            kw = client.add_draft.await_args.kwargs
            assert kw["title"] == "库里的首图标题"
            assert kw["html"] == "库里正文"
        await engine.dispose()

    asyncio.run(_run())


def test_push_newspic_no_images_gives_actionable_error(monkeypatch):
    async def _run():
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, [])
            res = await B.push_wechat_draft(
                title="标题", article_type="newspic", text="首图\n\n正文",
                content_id="c1", ctx={"db": s, "owner": "default"},
            )
            assert res["ok"] is False and "配图" in res["hint"], res
            assert client.add_draft.await_count == 0
        await engine.dispose()

    asyncio.run(_run())


def test_push_newspic_reuses_media_id(tmp_path, monkeypatch):
    """二次推送原地更新；40007 退回新建（与图文链路一致）"""

    async def _run():
        p = tmp_path / "a.png"
        p.write_bytes(b"x")
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, [str(p)], media_id="M_UPD")
            res = await B.push_wechat_draft(
                title="标题", article_type="newspic", text="首图\n\n正文",
                content_id="c1", ctx={"db": s, "owner": "default"},
            )
            assert res["ok"] is True and res.get("replaced") is True, res
            assert client.update_draft.await_count == 1
            assert client.add_draft.await_count == 0
            assert client.update_draft.await_args.kwargs["media_id"] == "M_UPD"

            client.update_draft.return_value = {
                "ok": False, "errcode": 40007, "error": "invalid media_id"
            }
            res2 = await B.push_wechat_draft(
                title="标题", article_type="newspic", text="首图\n\n正文",
                content_id="c1", ctx={"db": s, "owner": "default"},
            )
            assert res2["ok"] is True and res2.get("replaced") is False, res2
            assert client.add_draft.await_count == 1
        await engine.dispose()

    asyncio.run(_run())


def test_push_newspic_caps_uploads_at_6(tmp_path, monkeypatch):
    """配了 22 张也只上传前 6 张，并明确告诉用户截掉了几张"""

    async def _run():
        paths = []
        for i in range(22):
            p = tmp_path / f"p{i}.png"
            p.write_bytes(b"x")
            paths.append(str(p))
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, paths)
            res = await B.push_wechat_draft(
                title="标题", article_type="newspic", text="首图\n\n正文",
                content_id="c1", ctx={"db": s, "owner": "default"},
            )
            assert res["ok"] is True and res["image_count"] == 6, res
            assert client.upload_thumb.await_count == 6
            assert res["total_images"] == 22 and res["truncated_images"] == 16
            assert "只用前 6 张" in res["warning"]
        await engine.dispose()

    asyncio.run(_run())


def test_news_branch_unaffected_by_newspic(monkeypatch):
    """默认 article_type=news，html 仍走转存 + 封面，不误入贴图分支"""

    async def _run():
        client = _fake_client()
        _patch_common(monkeypatch, client)
        engine, Maker = await _maker()
        async with Maker() as s:
            await _seed(s, [])
            res = await B.push_wechat_draft(
                title="标题", html="<p>正文</p>", content_id="c1",
                ctx={"db": s, "owner": "default"},
            )
            # 没配图 → 封面硬拦（news 老行为）
            assert res["ok"] is False and "封面" in res["error"], res
            assert client.transfer_inline_images.await_count == 1
            assert client.upload_thumb.await_count == 0
        await engine.dispose()

    asyncio.run(_run())
