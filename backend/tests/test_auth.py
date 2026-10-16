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
from app.core.config import settings
from app.core.security import link_url
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
    auth_mod._LOGIN_ATTEMPTS.clear()

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

    # 换个用户名不受旧计数影响（计数按用户名维度）
    auth_mod._LOGIN_ATTEMPTS.clear()
    assert _register(client, "carol", invite="test-invite").status_code == 200
    ok = _login(client, "carol")
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["username"] == "carol"


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

_AUTH_DEPS = ("current_owner", "current_owner_id", "current_user", "link_auth")


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
