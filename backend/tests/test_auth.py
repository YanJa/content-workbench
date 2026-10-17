"""用户认证模块测试：注册 / 登录 / me / 邀请码 / 限流 / 存量认领 / 直链签名 / 越权。

- 每个测试一套独立的临时文件库（NullPool，避免跨 event loop 共享连接），
  不碰开发库；
- voices / uploads / outputs 目录指向临时目录，不动真实数据；
- 限流计数器是模块级内存，fixture 里清空，测试间不串味；
- TestClient 刻意不进 `with`（不触发 lifespan）：lifespan 里的 init_db /
  recover_orphans 会经全局 engine 碰开发库，且 TestClient 的 portal loop
  与 pytest 主 loop 不是同一个，全局 engine 的连接池会跨 loop 报错。
  认证逻辑不依赖 lifespan，直接发请求即可。
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
from app.core.security import hash_password, link_url
from app.db.session import Base, get_db
from app.main import app
from app.models.content import Content, QueueItem
from app.models.conversation import Conversation
from app.models.renderjob import RenderJob
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
        # 注册现在只认 invite_codes 表（旧 .env 全局码在启动时被
        # import_legacy_code() 导入为一条不限次记录）。测试里模拟这一步：
        # 往临时库插一条不限次的 test-invite，等价于生产环境启动导入。
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
    monkeypatch.setattr(settings, "INVITE_CODE", "test-invite")
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "admin")
    # 固定预置管理员的初始密码，测试里才能登录它（PASSWORD 是测试统一口令）
    monkeypatch.setattr(settings, "ADMIN_INITIAL_PASSWORD", PASSWORD)
    # ensure_admin_exists() 走的是模块级 SessionLocal（lifespan 里没有请求
    # 上下文，拿不到 get_db 依赖）；测试里换成临时库的 maker，否则会碰开发库。
    monkeypatch.setattr(rbac_mod, "SessionLocal", Maker)
    # 邀请码内核同样走模块级 SessionLocal（consume/create/resolve），必须一起
    # 换成临时库，否则注册扣减会写到开发库的 invite_codes 表。
    from app.core import invite as invite_mod

    monkeypatch.setattr(invite_mod, "SessionLocal", Maker)
    auth_mod._ATTEMPTS.clear()

    yield SimpleNamespace(tmp=tmp_path, Maker=Maker)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _register(client, username="alice", password=PASSWORD, invite=""):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invite},
    )


def _login(client, username="alice", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _split(url):
    """把 link_url 的产物拆成 (path, query)。"""
    path, _, query = url.partition("?")
    return path, query


async def _seed_legacy(Maker):
    """造一份 owner='default' 的存量数据 + 旧式直链 URL。"""
    async with Maker() as s:
        s.add(
            Content(
                id="c1",
                platform="gzh",
                kind="text",
                title="存量内容",
                body="正文",
                owner="default",
                images=[
                    {
                        "id": "i1",
                        "path": "gen/c1/x.png",
                        "url": "/api/content/images/c1/x.png?owner=default",
                        "alt": "配图",
                        "source": "gen",
                    }
                ],
                body_html='<p><img src="/api/content/images/c1/x.png?owner=default"></p>',
            )
        )
        s.add(
            QueueItem(
                id="q1", content_id="c1", platform="gzh", title="存量内容", status="ready", owner="default"
            )
        )
        s.add(Conversation(id="conv1", title="旧会话", owner="default"))
        await s.commit()


async def _add_user(Maker, username, *, role="member", is_active=True, created_at=1000, uid=None):
    """绕过 API 直接插一个用户（测 ensure_admin_exists 需要预置各种格局）。"""
    from app.models.user import User

    async with Maker() as s:
        u = User(
            id=uid or (username * 32)[:32],
            username=username,
            password_hash=hash_password(PASSWORD),
            role=role,
            is_active=is_active,
            created_at=created_at,
        )
        s.add(u)
        await s.commit()
        return u.id


async def _get_users(Maker):
    from sqlalchemy import select

    from app.models.user import User

    async with Maker() as s:
        rows = (await s.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
        return [{"id": u.id, "username": u.username, "role": u.role, "is_active": bool(u.is_active)}
                for u in rows]


def _admin_token(client, username="admin"):
    """预置管理员账号（启动时 lifespan 干的事）并登录，返回 token。"""
    asyncio.run(rbac_mod.ensure_admin_account())
    resp = _login(client, username)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ---------------------------------------------------------------- 注册
def test_register_first_user_and_claim(env):
    asyncio.run(_seed_legacy(env.Maker))
    # 旧音色目录（迁移对象）
    legacy_profile = env.tmp / "voices" / "profiles" / "p1"
    legacy_profile.mkdir(parents=True)
    (legacy_profile / "voice.json").write_text("{}", encoding="utf-8")

    client = TestClient(app)
    resp = _register(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"] and body["user"]["username"] == "alice"
    uid = body["user"]["id"]

    async def _check():
        async with env.Maker() as s:
            c = await s.get(Content, "c1")
            q = await s.get(QueueItem, "q1")
            conv = await s.get(Conversation, "conv1")
            assert c.owner == uid and q.owner == uid and conv.owner == uid
            # URL 重签：o=uid&sig=，且旧 owner=default 消失
            assert c.images[0]["url"].startswith("/api/content/images/c1/x.png?o=")
            assert "&sig=" in c.images[0]["url"]
            assert "owner=default" not in c.body_html
            assert f"?o={uid}&sig=" in c.body_html

    asyncio.run(_check())

    # 音色目录搬迁
    assert (env.tmp / "voices" / "owners" / uid / "profiles" / "p1" / "voice.json").is_file()
    assert not (env.tmp / "voices" / "profiles").exists()


def test_register_invite_mode(env):
    client = TestClient(app)
    assert _register(client).status_code == 200  # 首用户免码
    assert _register(client, "bob", invite="").status_code == 403
    assert _register(client, "bob", invite="wrong").status_code == 403
    assert _register(client, "bob", invite="test-invite").status_code == 200


def test_register_duplicate_username(env):
    client = TestClient(app)
    assert _register(client).status_code == 200
    assert _register(client).status_code == 409


def test_register_validation(env):
    client = TestClient(app)
    assert _register(client, "a").status_code == 422  # 用户名太短
    assert _register(client, "alice", "short").status_code == 422  # 密码太短


# ---------------------------------------------------------------- 登录
def test_login_success_failure_and_rate_limit(env):
    client = TestClient(app)
    assert _register(client).status_code == 200
    for _ in range(5):
        assert _login(client, password="wrong").status_code == 401
    # 第 6 次触发限流（先于密码校验）
    assert _login(client, password="wrong").status_code == 429
    # 限流期间正确密码也进不来
    assert _login(client).status_code == 429

    # 换用户名之前是「新配额」，现在 IP 维度会一并累计（见下面的
    # test_login_ip_rate_limit_blocks_rotation）——所以这里先清计数器再验登录本身
    auth_mod._ATTEMPTS.clear()
    assert _register(client, "carol", invite="test-invite").status_code == 200
    ok = _login(client, "carol")
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["username"] == "carol"


def test_login_ip_rate_limit_blocks_rotation(env):
    """换着用户名撞库也要挡得住。

    只按用户名限流时，攻击者每换一个名字就拿到新配额，等于没限 ——
    这条锁的是 IP 维度（nginx basic auth 摘掉后登录接口直接暴露，靠这档补位）。
    每个用户名只错 1 次（用户名档要 5 次才触发），只有 IP 会累计。
    """
    client = TestClient(app)
    auth_mod._ATTEMPTS.clear()
    assert _register(client).status_code == 200  # 首用户，注册不限流

    blocked_at = None
    for i in range(auth_mod._MAX_PER_IP + 5):
        r = _login(client, username=f"ghost{i}", password="wrong")
        if r.status_code == 429:
            blocked_at = i + 1
            break
        assert r.status_code == 401, f"第 {i + 1} 次意外返回 {r.status_code}"
    assert blocked_at == auth_mod._MAX_PER_IP + 1, (
        f"IP 维度限流没生效（第 {blocked_at} 次才拦下，期望 {auth_mod._MAX_PER_IP + 1}）"
    )


def test_register_rate_limit_on_bad_invite_code(env):
    """撞邀请码会被按 IP 限流；首用户注册豁免（不能被挡在系统门外）。"""
    client = TestClient(app)
    auth_mod._ATTEMPTS.clear()
    # 首用户：免码且不限流
    assert _register(client, "alice").status_code == 200

    auth_mod._ATTEMPTS.clear()
    blocked_at = None
    for i in range(auth_mod._MAX_PER_IP + 5):
        r = _register(client, f"mallory{i}", invite="wrong-code")
        if r.status_code == 429:
            blocked_at = i + 1
            break
        assert r.status_code == 403, f"第 {i + 1} 次意外返回 {r.status_code}"
    assert blocked_at == auth_mod._MAX_PER_IP + 1, (
        f"撞邀请码未被限流（第 {blocked_at} 次才拦下）"
    )


# ---------------------------------------------------------------- me / 401
def test_me_and_missing_token(env):
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    token = _register(client).json()["token"]
    me = client.get("/api/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_business_endpoint_requires_auth(env):
    client = TestClient(app)
    assert client.get("/api/usage/summary").status_code == 401
    token = _register(client).json()["token"]
    assert client.get("/api/usage/summary", headers=_auth(token)).status_code == 200


# ---------------------------------------------------------------- 直链签名
def test_link_signature(env):
    client = TestClient(app)
    body = _register(client).json()
    token, uid = body["token"], body["user"]["id"]

    img_dir = env.tmp / "uploads" / "gen" / "c1"
    img_dir.mkdir(parents=True)
    (img_dir / "x.png").write_bytes(b"fake-png")

    async def _seed():
        async with env.Maker() as s:
            s.add(
                Content(
                    id="c1",
                    platform="gzh",
                    kind="text",
                    title="有图",
                    body="正文",
                    owner=uid,
                    images=[{"id": "i1", "path": "gen/c1/x.png", "url": "", "source": "gen"}],
                )
            )
            await s.commit()

    asyncio.run(_seed())

    path, query = _split(link_url("/api/content/images/c1/x.png", uid))
    # 1) 合法签名 → 200
    assert client.get(f"{path}?{query}").status_code == 200
    # 2) 篡改签名 → 404
    bad = query.replace("sig=", "sig=0")
    assert client.get(f"{path}?{bad}").status_code == 404
    # 3) 匿名无签名 → 404
    assert client.get(path).status_code == 404
    # 4) 带 Bearer 头（无签名参数）→ 200
    assert client.get(path, headers=_auth(token)).status_code == 200
    # 5) 签名对应用户不存在 → 404
    p2, q2 = _split(link_url("/api/content/images/c1/x.png", "f" * 32))
    assert client.get(f"{p2}?{q2}").status_code == 404


def test_serve_output_owner_check(env, monkeypatch):
    """成片直链：get_job 内部直连 SessionLocal（不经依赖注入），
    测试里把它换成测试库的 Maker，否则查的是开发库。"""
    client = TestClient(app)
    alice = _register(client).json()
    bob = _register(client, "bob", invite="test-invite").json()

    import app.services.render as render_mod

    monkeypatch.setattr(render_mod, "SessionLocal", env.Maker)

    async def _seed():
        async with env.Maker() as s:
            s.add(
                RenderJob(
                    id="job1",
                    owner=alice["user"]["id"],
                    title="我的成片",
                    status="done",
                    output_path="job1.mp4",
                )
            )
            await s.commit()

    asyncio.run(_seed())
    (env.tmp / "outputs").mkdir(parents=True, exist_ok=True)
    (env.tmp / "outputs" / "job1.mp4").write_bytes(b"fake-mp4")

    path, query = _split(link_url("/api/video/outputs/job1.mp4", alice["user"]["id"]))
    assert client.get(f"{path}?{query}").status_code == 200

    # bob 持自己身份（Bearer）访问 alice 的 job → job 归属校验拦下 → 404
    r = client.get(path, headers=_auth(bob["token"]))
    assert r.status_code == 404


# ---------------------------------------------------------------- slides 撞目录
def test_slide_claim_cross_user(env):
    """同一份 PPT（folder 相同）被两个用户上传：先到先得，后到 404。"""
    client = TestClient(app)
    alice = _register(client).json()
    bob = _register(client, "bob", invite="test-invite").json()

    folder = "a" * 16
    slide_dir = env.tmp / "outputs" / "slides" / folder
    slide_dir.mkdir(parents=True)
    (slide_dir / "page_1.png").write_bytes(b"fake-slide")

    path = f"/api/video/slide-images/{folder}/page_1.png"
    assert client.get(path).status_code == 404  # 匿名

    p, q = _split(link_url(path, alice["user"]["id"]))
    assert client.get(f"{p}?{q}").status_code == 200  # alice 首次访问：登记归属并放行

    # bob 持自己身份访问同一 folder → 归属不符 → 404
    assert client.get(p, headers=_auth(bob["token"])).status_code == 404
    # bob 用自己的签名 URL 也不行（claim 里记的是 alice）
    p2, q2 = _split(link_url(path, bob["user"]["id"]))
    assert client.get(f"{p2}?{q2}").status_code == 404
    # alice 再访问仍然 200（幂等）
    assert client.get(f"{p}?{q}").status_code == 200


# ---------------------------------------------------------------- layout job 归属
def test_layout_job_status_owner(env, monkeypatch):
    client = TestClient(app)
    alice = _register(client).json()
    bob = _register(client, "bob", invite="test-invite").json()

    async def _seed():
        async with env.Maker() as s:
            s.add(
                Content(
                    id="c1",
                    platform="gzh",
                    kind="text",
                    title="x",
                    body="b",
                    owner=alice["user"]["id"],
                )
            )
            await s.commit()

    asyncio.run(_seed())

    from app.routers import layout as layout_mod

    monkeypatch.setattr(
        layout_mod, "get_layout_job", lambda task_id: {"content_id": "c1", "status": "done"}
    )

    url = "/api/content/items/c1/layout/t1"
    assert client.get(url).status_code == 401  # 匿名
    assert client.get(url, headers=_auth(alice["token"])).status_code == 200
    # bob 看 alice 的内容任务：归属校验先拦 → 404（不泄露存在性）
    assert client.get(url, headers=_auth(bob["token"])).status_code == 404


# ---------------------------------------------------------------- RBAC
def test_registered_users_are_always_members(env):
    """注册进来的一律 member —— 管理员是系统预置的，不靠注册顺序决定。

    不做「第一个注册的人自动当管理员」：那等于谁先打开页面谁拿管理员，
    演示时方便，真机上危险。
    """
    client = TestClient(app)
    first = _register(client, "alice").json()
    assert first["user"]["role"] == "member", first

    second = _register(client, "bob", invite="test-invite").json()
    assert second["user"]["role"] == "member", second


def test_me_returns_role_and_active(env):
    client = TestClient(app)
    token = _admin_token(client)
    me = client.get("/api/auth/me", headers=_auth(token)).json()
    assert me["role"] == "admin" and me["is_active"] is True


def test_ensure_admin_account_creates_it_when_missing(env):
    """库里没有同名账号 → 建一个 admin，且能用配置的初始密码登录。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "ADMIN_INITIAL_PASSWORD", "init-pass-2026")
    try:
        asyncio.run(rbac_mod.ensure_admin_account())
    finally:
        monkeypatch.undo()

    users = asyncio.run(_get_users(env.Maker))
    assert [u["username"] for u in users] == ["admin"]
    assert users[0]["role"] == "admin" and users[0]["is_active"] is True

    client = TestClient(app)
    assert _login(client, "admin", "init-pass-2026").status_code == 200


def test_ensure_admin_account_generates_password_when_unset(env, monkeypatch):
    """没配初始密码 → 随机生成一个（不会建出个空密码 / 弱口令的账号）。"""
    monkeypatch.setattr(settings, "ADMIN_INITIAL_PASSWORD", "")
    asyncio.run(rbac_mod.ensure_admin_account())

    users = asyncio.run(_get_users(env.Maker))
    assert users[0]["username"] == "admin" and users[0]["role"] == "admin"

    client = TestClient(app)
    # 随机口令 ≠ fixture 里的测试统一口令，所以这里必须进不去
    assert _login(client, "admin", PASSWORD).status_code == 401


def test_ensure_admin_account_promotes_existing_without_touching_password(env):
    """同名账号已存在（比如用户自己注册的）→ 只摆正角色，**绝不动密码**。

    改密码等于把人锁在自己的系统门外 —— 那是他自己设的。
    """
    asyncio.run(_add_user(env.Maker, "admin", role="member", created_at=1000))
    asyncio.run(_add_user(env.Maker, "bob", role="member", created_at=2000))

    asyncio.run(rbac_mod.ensure_admin_account())

    users = asyncio.run(_get_users(env.Maker))
    assert next(u for u in users if u["username"] == "admin")["role"] == "admin"
    assert next(u for u in users if u["username"] == "bob")["role"] == "member"

    # 密码仍是注册时那个
    client = TestClient(app)
    assert _login(client, "admin").status_code == 200

    # 幂等：再跑一次不报错、不多造人
    asyncio.run(rbac_mod.ensure_admin_account())
    assert len(asyncio.run(_get_users(env.Maker))) == 2


def test_admin_endpoints_require_admin(env):
    client = TestClient(app)
    admin_token = _admin_token(client)
    member_token = _register(client, "bob", invite="test-invite").json()["token"]

    assert client.get("/api/admin/users", headers=_auth(admin_token)).status_code == 200
    assert client.get("/api/admin/stats", headers=_auth(admin_token)).status_code == 200

    r = client.get("/api/admin/users", headers=_auth(member_token))
    assert r.status_code == 403, r.text
    assert client.get("/api/admin/stats", headers=_auth(member_token)).status_code == 403
    # 没带令牌 → 401（身份都没有，轮不到判角色）
    assert client.get("/api/admin/users").status_code == 401


def test_admin_create_user_bypasses_invite_code(env):
    client = TestClient(app)
    admin_token = _admin_token(client)

    r = client.post(
        "/api/admin/users",
        json={"username": "bob", "password": PASSWORD, "role": "member"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "member"
    # 代建的账号没邀请码也能登录
    assert _login(client, "bob").status_code == 200

    assert client.post(
        "/api/admin/users",
        json={"username": "bob", "password": PASSWORD},
        headers=_auth(admin_token),
    ).status_code == 409
    assert client.post(
        "/api/admin/users",
        json={"username": "carol", "password": "short"},
        headers=_auth(admin_token),
    ).status_code == 422


def test_deactivated_user_loses_access_immediately(env):
    """停用即时生效：不进 JWT，所以旧 token 下一次请求就 401。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    bob = _register(client, "bob", invite="test-invite").json()
    bob_id, bob_token = bob["user"]["id"], bob["token"]

    assert client.get("/api/queue/items", headers=_auth(bob_token)).status_code == 200

    r = client.patch(
        f"/api/admin/users/{bob_id}",
        json={"is_active": False},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200 and r.json()["is_active"] is False

    # 不等 token 过期，当场失效
    denied = client.get("/api/queue/items", headers=_auth(bob_token))
    assert denied.status_code == 401, denied.text
    assert "停用" in denied.json()["detail"]

    # 恢复启用后又能用
    client.patch(f"/api/admin/users/{bob_id}", json={"is_active": True}, headers=_auth(admin_token))
    assert client.get("/api/queue/items", headers=_auth(bob_token)).status_code == 200


def test_deactivated_login_has_distinct_message(env):
    """被停用的人登录，提示必须和「密码错误」区分开，否则他会一直重试。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    bob_id = _register(client, "bob", invite="test-invite").json()["user"]["id"]
    client.patch(f"/api/admin/users/{bob_id}", json={"is_active": False}, headers=_auth(admin_token))

    r = _login(client, "bob")
    assert r.status_code == 401
    assert r.json()["detail"] == "账号已被停用，请联系管理员"
    # 密码不对时仍是原来的文案
    assert _login(client, "bob", password="wrong").json()["detail"] == "用户名或密码错误"


def test_admin_cannot_modify_self(env):
    """不能对自己动手：降级/停用自己都会把系统变成没有管理员的孤儿。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    my_id = client.get("/api/auth/me", headers=_auth(admin_token)).json()["id"]

    for payload in ({"role": "member"}, {"is_active": False}, {"role": "admin"}):
        r = client.patch(
            f"/api/admin/users/{my_id}", json=payload, headers=_auth(admin_token)
        )
        assert r.status_code == 400, (payload, r.text)
        assert "自己" in r.json()["detail"]


def test_admin_list_shows_everyone_and_self_target_is_blocked(env):
    """用户列表能看到所有人；但对自己下手一律被拒（含停用、降级、改角色）。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    _register(client, "bob", invite="test-invite")

    users = client.get("/api/admin/users", headers=_auth(admin_token)).json()
    assert {u["username"] for u in users} == {"admin", "bob"}

    bob_id = next(u["id"] for u in users if u["username"] == "bob")
    my_id = next(u["id"] for u in users if u["username"] == "admin")

    # 停用别人：可以
    assert client.patch(f"/api/admin/users/{bob_id}", json={"is_active": False},
                        headers=_auth(admin_token)).status_code == 200
    # 停用自己：不行（否则系统只剩停用的人，等于把自己锁在门外）
    r = client.patch(f"/api/admin/users/{my_id}", json={"is_active": False},
                     headers=_auth(admin_token))
    assert r.status_code == 400 and "自己" in r.json()["detail"]


def test_guard_keeps_at_least_one_admin(env):
    """「至少保留一名管理员」是兜底防线 —— 直接测 _guard 本身。

    走 HTTP 接口时，操作者自己必然也是 admin，所以「不能对自己」那条会先
    拦下，系统事实上不可能被清空管理员。这条守卫防的是以后加了「批量改
    角色 / 删除用户」之类绕过单点操作的接口之后出洞。
    """
    from fastapi import HTTPException

    from app.models.user import User
    from app.routers import admin as admin_mod

    async def _scenario():
        async with env.Maker() as db:
            admin_id = await _add_user(env.Maker, "boss", role="admin", uid="a" * 32)
            member_id = await _add_user(env.Maker, "bob", role="member", uid="b" * 32)
            target = await db.get(User, admin_id)
            operator = await db.get(User, member_id)

            # 只有 target 一个 admin → 降级他必须被拒
            try:
                await admin_mod._guard(db, target, operator, new_role="member")
            except HTTPException as e:
                assert e.status_code == 400 and "至少" in e.detail
            else:
                raise AssertionError("唯一 admin 被降级却没拦下")

            # 同样是唯一 admin，停用也要拦
            try:
                await admin_mod._guard(db, target, operator, deactivate=True)
            except HTTPException as e:
                assert e.status_code == 400 and "至少" in e.detail
            else:
                raise AssertionError("唯一 admin 被停用却没拦下")

            # 再加一个 admin → 允许（losing_admin 但人数够）
            await _add_user(env.Maker, "carol", role="admin", uid="c" * 32)
            await admin_mod._guard(db, target, operator, new_role="member")

    asyncio.run(_scenario())


def test_invalid_role_value_rejected(env):
    """非法角色值不能落库：下次启动 ensure_admin_exists 找不到 admin 会乱升人。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    bob_id = _register(client, "bob", invite="test-invite").json()["user"]["id"]

    r = client.patch(f"/api/admin/users/{bob_id}", json={"role": "root"}, headers=_auth(admin_token))
    assert r.status_code == 400 and "非法的角色值" in r.json()["detail"]

    r2 = client.post(
        "/api/admin/users",
        json={"username": "carol", "password": PASSWORD, "role": "root"},
        headers=_auth(admin_token),
    )
    assert r2.status_code == 400
    # 空 body 也拒绝，免得「改了个寂寞」还回 200
    assert client.patch(f"/api/admin/users/{bob_id}", json={}, headers=_auth(admin_token)).status_code == 400


def test_admin_stats_is_site_wide(env):
    """全站聚合：不能复用 usage_svc.summary —— 它 owner=None 会 fallback 查 'default'。"""
    client = TestClient(app)
    admin_token = _admin_token(client)
    uid = client.get("/api/auth/me", headers=_auth(admin_token)).json()["id"]

    async def _seed():
        from app.models.usage import LlmUsage

        async with env.Maker() as s:
            s.add(Content(id="c9", platform="gzh", kind="text", title="t", body="b", owner=uid))
            s.add(LlmUsage(owner=uid, model="deepseek-flash", prompt_tokens=100,
                           completion_tokens=50, total_tokens=150))
            await s.commit()

    asyncio.run(_seed())

    stats = client.get("/api/admin/stats", headers=_auth(admin_token)).json()
    assert stats["users"]["total"] == 1 and stats["users"]["admins"] == 1
    assert stats["contents"] == 1
    assert stats["llm"]["calls"] == 1 and stats["llm"]["total_tokens"] == 150
    assert stats["by_user"][0]["username"] == "admin" and stats["by_user"][0]["contents"] == 1


def test_admin_cannot_touch_others_content(env):
    """管理员也看不到别人的内容 —— owner 隔离对 admin 一样生效。

    这是本轮刻意守住的边界：给 admin 开跨用户读取，等于先制造风险再防御风险。
    """
    client = TestClient(app)
    admin_token = _admin_token(client)
    bob = _register(client, "bob", invite="test-invite").json()
    bob_id, bob_token = bob["user"]["id"], bob["token"]

    async def _seed():
        async with env.Maker() as s:
            s.add(Content(id="c1", platform="gzh", kind="text", title="bob 的稿子",
                          body="私密", owner=bob_id))
            await s.commit()

    asyncio.run(_seed())

    # bob 自己能看
    assert client.get("/api/content/items/c1", headers=_auth(bob_token)).status_code == 200
    # admin 看 bob 的稿子 → 404（不泄露存在性），而不是 200
    assert client.get("/api/content/items/c1", headers=_auth(admin_token)).status_code == 404
    # 列表也只返回自己的
    assert client.get("/api/content/items", headers=_auth(admin_token)).json() == []


# ---------------------------------------------------------------- 端点鉴权回归
# 新增端点时如果忘了挂 owner 依赖，这个测试会失败 —— 白名单要显式写理由。
PUBLIC_ENDPOINTS = {
    ("/api/auth/register", "POST"): "注册，认证自身",
    ("/api/auth/login", "POST"): "登录，认证自身",
    ("/api/health", "GET"): "健康检查，部署探活用",
    ("/api/video/voices", "GET"): "edge-tts 全局预置音色清单，无用户数据",
    ("/api/video/voices/provider", "GET"): "复刻后端状态，全局配置",
    ("/api/tools", "GET"): "内置工具清单，全局静态",
    ("/api/usage/pricing", "GET"): "模型单价表，全局静态",
    ("/api/usage/estimate", "GET"): "成本估算，纯计算不落库",
}

# require_admin：管理面端点。它内部就 Depends(current_user)，所以带它的端点
# 天然也有身份校验 —— 单列出来是因为端点源码里只出现这个名字。
_AUTH_DEPS = ("current_owner", "current_owner_id", "current_user", "link_auth", "require_admin")


def test_every_api_endpoint_is_guarded():
    """扫全部 /api 端点：要么带鉴权依赖，要么在公开白名单里。"""
    import ast
    import inspect

    import app.main as main_mod

    # 从 app.routes 拿真实注册的路径，比 AST 扫源码更可靠
    unguarded = []
    for route in main_mod.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or not path.startswith("/api"):
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            src = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            if (path, m) in PUBLIC_ENDPOINTS:
                continue
            if any(d in src for d in _AUTH_DEPS):
                continue
            unguarded.append(f"{m:6} {path}  ({endpoint.__module__}.{endpoint.__name__})")

    # 公开白名单里的端点必须真的存在，免得白名单留着僵尸条目
    live = {(r.path, m) for r in main_mod.app.routes
            for m in (getattr(r, "methods", None) or set())}
    stale = [f"{p} {m}" for (p, m) in PUBLIC_ENDPOINTS if (p, m) not in live]

    assert not unguarded, "以下端点缺少鉴权依赖（补 Depends(current_owner_id) 或加进白名单）：\n" + "\n".join(unguarded)
    assert not stale, f"白名单里有不存在的端点：{stale}"
