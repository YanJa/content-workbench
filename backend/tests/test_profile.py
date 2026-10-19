"""个人资料接口测试：PUT /auth/profile（昵称 / 邮箱）。

沿用 test_auth.py 的基建：每测试一套独立临时文件库（NullPool），
TestClient 不进 `with`（不触发 lifespan），限流计数器清空。
只测 profile 端点自身的行为，不重复认证链路的用例。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到所有表
from app.core import rbac as rbac_mod
from app.core.config import settings
from app.db.session import Base, get_db
from app.main import app
from app.routers import auth as auth_mod

PASSWORD = "password123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Maker() as s:
            from app.models.invite import InviteCode

            s.add(InviteCode(code="test-invite", note="测试码", max_uses=None))
            await s.commit()

    asyncio.run(_init())

    async def _override_get_db():
        async with Maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(settings, "VOICE_DIR", str(tmp_path / "voices"))
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(settings, "AUTH_REGISTER_MODE", "invite")
    monkeypatch.setattr(rbac_mod, "SessionLocal", Maker)
    from app.core import invite as invite_mod

    monkeypatch.setattr(invite_mod, "SessionLocal", Maker)
    auth_mod._ATTEMPTS.clear()

    yield SimpleNamespace(tmp=tmp_path, Maker=Maker)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _register(client, username="alice"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD, "invite_code": "test-invite"},
    )


def _me(client, token):
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


def _save(client, token, **payload):
    return client.put(
        "/api/auth/profile",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_update_nickname_and_email(env):
    """昵称 + 邮箱都能改，且 /auth/me 立刻返回新值（前端顶栏据此刷新）。"""
    token = _register(client := TestClient(app)).json()["token"]

    r = _save(client, token, nickname="甲木", email="jiamu@example.com")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nickname"] == "甲木"
    assert body["email"] == "jiamu@example.com"

    me = _me(client, token)
    assert me.json()["nickname"] == "甲木"
    assert me.json()["email"] == "jiamu@example.com"


def test_reject_bad_email(env):
    """邮箱格式不对直接 422，不落库（nickname 一起提交也不能半途生效）。"""
    token = _register(client := TestClient(app)).json()["token"]

    r = _save(client, token, nickname="甲木", email="not-an-email")
    assert r.status_code == 422, r.text
    # 整单回滚：me 里两个字段都还是原样
    me = _me(client, token).json()
    assert me["nickname"] is None
    assert me["email"] is None


def test_email_taken_by_other(env):
    """邮箱被别的账号占了 → 409；自己重复保存同一个邮箱不受限。"""
    alice = _register(client := TestClient(app), "alice").json()["token"]
    bob = _register(client, "bob").json()["token"]

    assert _save(client, alice, email="shared@example.com").status_code == 200
    r = _save(client, bob, email="shared@example.com")
    assert r.status_code == 409, r.text

    # alice 再存一次同一个邮箱：排除自己，不误伤
    assert _save(client, alice, email="shared@example.com").status_code == 200


def test_clear_with_empty_string(env):
    """空串 = 清空，回到「未设置」（前端显示名回落 username）。"""
    token = _register(client := TestClient(app)).json()["token"]
    assert _save(client, token, nickname="甲木", email="jiamu@example.com").status_code == 200

    r = _save(client, token, nickname="", email="")
    assert r.status_code == 200
    assert r.json()["nickname"] is None
    assert r.json()["email"] is None


def test_omitted_field_kept(env):
    """只传一个字段时另一个保持原值（None = 不改，空串 = 清空，两者要分开）。"""
    token = _register(client := TestClient(app)).json()["token"]
    _save(client, token, nickname="甲木", email="jiamu@example.com")

    _save(client, token, nickname="改名了")
    me = _me(client, token).json()
    assert me["nickname"] == "改名了"
    assert me["email"] == "jiamu@example.com"


# ---------------------------------------------------------------- 头像
def _png_bytes(size: int = 300) -> bytes:
    """造一张带透明通道的 PNG（顺带覆盖「贴白底再转 JPEG」的分支）。"""
    import io

    from PIL import Image

    img = Image.new("RGBA", (size, size // 2), (255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _upload(client, token, data=None, filename="a.png", ctype="image/png"):
    return client.post(
        "/api/auth/avatar",
        files={"file": (filename, data if data is not None else _png_bytes(), ctype)},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_avatar_upload_and_serve(env):
    """上传 → avatar_url 是签名直链；带头像 URL 能取到图（且已是 JPEG）。"""
    import io

    from PIL import Image

    token = _register(client := TestClient(app)).json()["token"]
    assert _me(client, token).json()["avatar_url"] is None

    r = _upload(client, token)
    assert r.status_code == 200, r.text
    url = r.json()["avatar_url"]
    assert url and url.startswith("/api/auth/avatar/") and "sig=" in url

    # 直链取图（<img src> 场景：不带 Authorization，全靠签名）
    got = client.get(url)
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(got.content))
    # 原图 300×150 → 居中裁成 150×150 正方形；小于 256 **不放大**（放大只会糊）
    assert img.size == (150, 150)

    # /me 与 PUT /auth/profile 的返回都带头像（前端顶栏靠它渲染）
    assert _me(client, token).json()["avatar_url"] == url


def test_avatar_downscales_large_image(env):
    """大图缩到 256×256（恒定输出，换图靠 uid 固定名覆盖）。"""
    import io

    from PIL import Image

    token = _register(client := TestClient(app)).json()["token"]
    big = Image.new("RGB", (600, 400), (0, 128, 255))
    buf = io.BytesIO()
    big.save(buf, "PNG")

    url = _upload(client, token, data=buf.getvalue()).json()["avatar_url"]
    got = client.get(url)
    assert got.status_code == 200
    assert Image.open(io.BytesIO(got.content)).size == (256, 256)


def test_avatar_rejects_bad_input(env):
    """格式不对 / 超大 / 空文件 / 假图片（后缀对内容不对）都 400，不留脏状态。"""
    token = _register(client := TestClient(app)).json()["token"]

    assert _upload(client, token, filename="a.gif", ctype="image/gif").status_code == 400
    assert _upload(client, token, data=b"x" * (2 * 1024 * 1024 + 1)).status_code == 400
    assert _upload(client, token, data=b"").status_code == 400
    # 后缀是 png、内容是垃圾：Pillow 解析失败 → 400
    assert _upload(client, token, data=b"not an image").status_code == 400

    me = _me(client, token).json()
    assert me["avatar_url"] is None


def test_avatar_delete(env):
    """删除后回到 NULL；再取旧签名链接应该 404（文件也清了）。"""
    token = _register(client := TestClient(app)).json()["token"]
    url = _upload(client, token).json()["avatar_url"]

    r = client.delete("/api/auth/avatar", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["avatar_url"] is None

    assert client.get(url).status_code == 404


def test_avatar_url_is_owner_scoped(env):
    """签名绑 uid：拿 A 的头像链接冒充 B 的路径取不到图。"""
    alice = _register(client := TestClient(app), "alice").json()["token"]
    bob = _register(client, "bob").json()["token"]
    _upload(client, alice)

    alice_url = _me(client, alice).json()["avatar_url"]
    # 把链接里的 uid 换成 bob 的 —— 签名对不上，404（不是 403：不泄露存在性）
    bob_id = _me(client, bob).json()["id"]
    forged = alice_url.split("?")[0].replace(alice_url.split("/")[-2], bob_id)
    assert client.get(f"{forged}?{alice_url.split('?')[1]}").status_code == 404
