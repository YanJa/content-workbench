"""邀请码管理测试：生成 / 扣减 / 用尽 / 停用 / 渠道统计 / 删除保护。

覆盖四条主线：
- 注册扣减：限 n 次的码放行 n 次、第 n+1 次 403；不限次码一直放行；
- 管理端点：生成（自定义/随机/不限次）、停用后拒绝注册、启用恢复、
  删除只允许 used==0 的码；
- 渠道统计：注册用户带 invite_code，用户列表反查出 invite_note；
- 权限：非 admin 访问邀请码端点 403。

fixture 与 test_auth 一致：独立临时库 + 三处 SessionLocal 全 patch 到
临时库（漏一个就绕过测试库去碰开发库）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.core import invite as invite_mod
from app.core import rbac as rbac_mod
from app.core.config import settings
from app.db.session import Base, get_db
from app.main import app
from app.models.invite import InviteCode
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
    monkeypatch.setattr(settings, "INVITE_CODE", "legacy-code")
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "ADMIN_INITIAL_PASSWORD", PASSWORD)
    # 邀请码内核 + rbac 各自持有模块级 SessionLocal，都要 patch 到临时库。
    monkeypatch.setattr(invite_mod, "SessionLocal", Maker)
    monkeypatch.setattr(rbac_mod, "SessionLocal", Maker)
    auth_mod._ATTEMPTS.clear()

    # 预置一个 admin 账号（走临时库），管理端点要用。
    async def _seed_admin():
        from app.core.security import hash_password
        from app.models.user import User

        async with Maker() as s:
            s.add(User(username="admin", password_hash=hash_password(PASSWORD), role="admin"))
            await s.commit()

    asyncio.run(_seed_admin())

    yield SimpleNamespace(tmp=tmp_path, Maker=Maker)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _login_admin(client) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, username, invite, password=PASSWORD):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invite},
    )


def _create_code(client, token, note="渠道A", max_uses=None, code=None):
    body = {"note": note, "max_uses": max_uses}
    if code is not None:
        body["code"] = code
    return client.post("/api/admin/invite-codes", json=body, headers=_auth(token))


# ---------------------------------------------------------------- 生成
def test_create_invite_code_variants(env):
    client = TestClient(app)
    token = _login_admin(client)

    # 限次
    r = _create_code(client, token, note="限次", max_uses=3)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["code"] and d["note"] == "限次" and d["max_uses"] == 3 and d["used"] == 0
    assert d["exhausted"] is False

    # 不限次：max_uses 留空 / 0 / 负数都是不限
    for mu in (None, 0, -5):
        r = _create_code(client, token, note="不限", max_uses=mu)
        assert r.status_code == 200
        assert r.json()["max_uses"] is None

    # 自定义码
    r = _create_code(client, token, note="自定义", code="MYCODE")
    assert r.status_code == 200 and r.json()["code"] == "MYCODE"

    # 自定义码撞唯一约束 → 409
    r = _create_code(client, token, note="重复", code="MYCODE")
    assert r.status_code == 409

    # 非法自定义码（太短 / 含空格）→ 400
    assert _create_code(client, token, code="ab").status_code == 400
    assert _create_code(client, token, code="a b c").status_code == 400


# ---------------------------------------------------------------- 注册扣减
def test_register_consumes_limited_code(env):
    client = TestClient(app)
    token = _login_admin(client)
    code = _create_code(client, token, note="限3", max_uses=3).json()["code"]

    # 放行 3 次
    for i in range(3):
        r = _register(client, f"user{i}", code)
        assert r.status_code == 200, r.text
        assert r.json()["user"]["invite_code"] == code

    # 第 4 次拒绝
    assert _register(client, "user4", code).status_code == 403

    # 列表里 used=3、exhausted=True
    lst = client.get("/api/admin/invite-codes", headers=_auth(token)).json()
    item = next(c for c in lst if c["code"] == code)
    assert item["used"] == 3 and item["exhausted"] is True


def test_register_unlimited_code(env):
    client = TestClient(app)
    token = _login_admin(client)
    code = _create_code(client, token, note="不限", max_uses=None).json()["code"]

    for i in range(5):
        assert _register(client, f"user{i}", code).status_code == 200


def test_register_rejects_disabled_code(env):
    client = TestClient(app)
    token = _login_admin(client)
    code = _create_code(client, token, note="要停用").json()
    cid, ccode = code["id"], code["code"]

    # 停用后注册被拒
    assert client.post(f"/api/admin/invite-codes/{cid}/disable", headers=_auth(token)).status_code == 200
    assert _register(client, "nobody", ccode).status_code == 403

    # 启用后恢复
    assert client.post(f"/api/admin/invite-codes/{cid}/enable", headers=_auth(token)).status_code == 200
    assert _register(client, "somebody", ccode).status_code == 200


# ---------------------------------------------------------------- 渠道统计
def test_user_list_shows_invite_note(env):
    client = TestClient(app)
    token = _login_admin(client)
    code = _create_code(client, token, note="小红书投放").json()["code"]
    assert _register(client, "xiaohongshu_user", code).status_code == 200

    users = client.get("/api/admin/users", headers=_auth(token)).json()
    u = next(x for x in users if x["username"] == "xiaohongshu_user")
    assert u["invite_code"] == code
    assert u["invite_note"] == "小红书投放"


# ---------------------------------------------------------------- 删除保护
def test_delete_only_unused_code(env):
    client = TestClient(app)
    token = _login_admin(client)

    # 没用过的码可以删
    fresh = _create_code(client, token, note="可删").json()
    assert client.delete(f"/api/admin/invite-codes/{fresh['id']}", headers=_auth(token)).status_code == 204

    # 用过的码不能删（渠道统计要保留）
    used = _create_code(client, token, note="用过", max_uses=2).json()
    assert _register(client, "user_del", used["code"]).status_code == 200
    r = client.delete(f"/api/admin/invite-codes/{used['id']}", headers=_auth(token))
    assert r.status_code == 400
    assert "不能删除" in r.json()["detail"]


# ---------------------------------------------------------------- 权限
def test_invite_endpoints_require_admin(env):
    client = TestClient(app)
    token = _login_admin(client)
    code = _create_code(client, token, note="x").json()["code"]
    # 注册一个 member
    r = _register(client, "member1", code)
    mtoken = r.json()["token"]

    assert client.get("/api/admin/invite-codes", headers=_auth(mtoken)).status_code == 403
    assert client.post("/api/admin/invite-codes", json={}, headers=_auth(mtoken)).status_code == 403
    # 无 token 更该拒
    assert client.get("/api/admin/invite-codes").status_code == 401
