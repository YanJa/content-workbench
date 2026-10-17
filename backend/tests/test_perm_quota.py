"""细粒度权限（按钮级）+ 使用次数配额 的测试。

覆盖四条主线：

1. **权限内核**：角色模板是覆盖层不是唯一真源（空表不能锁死全站）、admin
   全开不可收窄、用户覆盖是「增量」不是「快照」；
2. **配额内核**：超限硬拦截且文案带重置时间、`limit` 缺失/0/负数 = 不限、
   日/月周期不串、按用户隔离、并发不超发；
3. **端点映射**：`/chat/confirm` 按 tool_name 映射到目标权限点（不设统一的
   「确认」权限，否则会出现「有 video.render 却点不了确认」的死锁）；
   推送失败退还配额；
4. **管理端点**：权限目录 / 角色模板 / 用户覆盖 / 人工重置。

每个测试一套独立临时库（NullPool），不碰开发库。TestClient 刻意不进 `with`
（不触发 lifespan），与 test_auth.py 同一套理由。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到所有表
from app.core import quota as quota_mod
from app.core import rbac as rbac_mod
from app.core.config import settings
from app.core.rbac import PERMISSION_KEYS, TOOL_PERM
from app.core.security import hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.user import User
from app.routers import auth as auth_mod
from app.routers import chat as chat_mod

PASSWORD = "password123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    # 测试库也要开 WAL + busy_timeout：并发扣减那条测试靠它才能排队而不是
    # 直接抛 database is locked（生产库的 pragma 见 db/session.py）。
    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001, ANN202
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 注册只认 invite_codes 表，插入一条不限次码等价于生产环境启动导入。
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
    monkeypatch.setattr(settings, "ADMIN_INITIAL_PASSWORD", PASSWORD)
    # 三个模块各自持有一份 SessionLocal 引用（require_use / quota / chat 的
    # 退还分支），漏一个就会绕过测试库去碰开发库。
    monkeypatch.setattr(rbac_mod, "SessionLocal", Maker)
    monkeypatch.setattr(quota_mod, "SessionLocal", Maker)
    monkeypatch.setattr(chat_mod, "SessionLocal", Maker)
    # 邀请码内核也用模块级 SessionLocal，注册扣减必须落在临时库。
    from app.core import invite as invite_mod

    monkeypatch.setattr(invite_mod, "SessionLocal", Maker)
    auth_mod._ATTEMPTS.clear()

    yield SimpleNamespace(tmp=tmp_path, Maker=Maker)

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


# ---------------------------------------------------------------- 小工具
def _register(client, username="alice", password=PASSWORD, invite="test-invite"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "invite_code": invite},
    )


def _login(client, username="alice", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _admin_token(client, username="admin"):
    """预置管理员（lifespan 干的事）并登录。"""
    asyncio.run(rbac_mod.ensure_admin_account())
    resp = _login(client, username)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _add_user(Maker, username, *, role="member", uid=None):
    async with Maker() as s:
        u = User(
            id=uid or (username * 32)[:32],
            username=username,
            password_hash=hash_password(PASSWORD),
            role=role,
        )
        s.add(u)
        await s.commit()
        return u.id


def _me(client, token):
    return client.get("/api/auth/me", headers=_auth(token)).json()


def _set_role_template(Maker, role, permissions=None, quotas=None):
    """直接写角色模板（绕过 HTTP，测试里更直白）。"""
    from app.models.permission import RolePermission

    async def _run():
        async with Maker() as s:
            row = (
                await s.execute(select(RolePermission).where(RolePermission.role == role))
            ).scalar_one_or_none()
            if row is None:
                row = RolePermission(role=role)
                s.add(row)
            row.permissions = permissions
            row.quotas = quotas
            await s.commit()

    asyncio.run(_run())


def _set_user_override(Maker, uid, perm_override=None, quota_override=None):
    from sqlalchemy import update

    from app.models.user import User as UserModel

    async def _run():
        async with Maker() as s:
            await s.execute(
                update(UserModel).where(UserModel.id == uid).values(
                    perm_override=perm_override, quota_override=quota_override
                )
            )
            await s.commit()

    asyncio.run(_run())


def _used(me: dict, feature: str) -> int:
    return me["usage"][feature]["used"]


# ================================================================ 权限内核
def test_member_defaults_to_all_functions_when_template_empty(env):
    """空模板不能锁死全站：代码常量才是兜底，表只是覆盖层。

    默认给**全部**权限点（含菜单级）：菜单可见性默认全开，管理员想收窄再去
    模板里取消勾选。反过来做会让「取消勾选」变成「默认就没有」—— 新成员的
    侧栏直接是空的，那是 fail-closed。
    """
    client = TestClient(app)
    me = _me(client, _register(client).json()["token"])
    assert me["role"] == "member"
    assert set(me["permissions"]) == set(PERMISSION_KEYS)
    # 菜单级也在内：侧栏默认全可见
    assert {"menu.chat", "menu.video", "menu.settings"} <= set(me["permissions"])
    # 没配过配额 → 一律「不限」
    for feature, snap in me["usage"].items():
        assert snap["unlimited"] is True and snap["limit"] is None, (feature, snap)


def test_role_template_narrows_member(env):
    """角色模板能收窄 member，且改完立即生效（下次请求现算）。"""
    _set_role_template(env.Maker, "member", permissions=["chat.send", "menu.chat"])

    client = TestClient(app)
    token = _register(client).json()["token"]
    me = _me(client, token)
    assert set(me["permissions"]) == {"chat.send", "menu.chat"}

    # 没权限的按钮级功能：端点直接 403（不是 404、不是静默成功）
    r = client.post(
        "/api/chat/confirm",
        json={"tool_call_id": "t1", "tool_name": "push_wechat_draft", "arguments": {}},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    assert "权限" in r.json()["detail"]


def test_role_template_change_reaches_users_without_override(env):
    """改模板影响的是「没被单独覆盖的人」—— 模板升级要能传下去。"""
    client = TestClient(app)
    token = _register(client).json()["token"]
    assert "video.render" in _me(client, token)["permissions"]

    # 管理员从模板里拿掉 video.render
    _set_role_template(env.Maker, "member", permissions=["chat.send"])
    assert "video.render" not in _me(client, token)["permissions"]


def test_user_override_is_incremental_not_snapshot(env):
    """用户覆盖是**增量**：之后模板新增的功能，他照样能收到。

    快照式覆盖会把人钉死在管理员改他那一刻的状态上 —— 模板一升级，
    被覆盖过的用户永远收不到新功能，而且没人想得到是因为这个。
    """
    client = TestClient(app)
    token = _register(client).json()["token"]
    uid = _me(client, token)["id"]

    # 先给他单独开一个默认没有的权限点
    _set_role_template(env.Maker, "member", permissions=["chat.send"])
    _set_user_override(env.Maker, uid, perm_override={"grant": ["video.render"], "deny": []})
    perms = _me(client, token)["permissions"]
    assert set(perms) == {"chat.send", "video.render"}

    # 模板后来加了 layout.run：没被 deny 掉，所以他跟着拿到
    _set_role_template(env.Maker, "member", permissions=["chat.send", "layout.run"])
    assert set(_me(client, token)["permissions"]) == {"chat.send", "video.render", "layout.run"}

    # deny 仍然说了算（覆盖块是全量替换：这次没再 grant video.render，
    # 它就跟着模板一起没了 ——  UI 上的勾选矩阵也是按「有效权限」整体保存的）
    _set_user_override(
        env.Maker, uid, perm_override={"grant": ["video.render"], "deny": ["chat.send"]}
    )
    assert set(_me(client, token)["permissions"]) == {"video.render", "layout.run"}


def test_admin_permissions_are_all_and_cannot_be_narrowed(env):
    """admin 全开，且**不可收窄** —— 最常见的自锁方式就是把自己勾空。"""
    client = TestClient(app)
    admin = _admin_token(client)
    assert set(_me(client, admin)["permissions"]) == set(PERMISSION_KEYS)

    # 改 admin 角色模板的权限清单 → 400
    r = client.put(
        "/api/admin/role-permissions/admin",
        json={"permissions": ["chat.send"]},
        headers=_auth(admin),
    )
    assert r.status_code == 400 and "锁" in r.json()["detail"]

    # 给 admin 单独设权限覆盖 → 同样 400（覆盖了对 admin 也不生效，不如说清）
    uid = _me(client, admin)["id"]
    r2 = client.put(
        f"/api/admin/users/{uid}/overrides",
        json={"perm_override": {"grant": [], "deny": PERMISSION_KEYS[:1]}},
        headers=_auth(admin),
    )
    assert r2.status_code == 400 and "管理员" in r2.json()["detail"]

    # 但配额照走：给 admin 角色配一个对话额度，必须真的生效（否则 UI 说谎）
    _set_role_template(env.Maker, "admin", quotas={"chat": {"period": "day", "limit": 1}})
    await_consumed = asyncio.run(quota_mod.consume(uid, "chat", {"period": "day", "limit": 1}))
    assert await_consumed is None  # 没超限，静默通过
    r3 = client.post(
        "/api/chat/stream",
        json={"message": "在吗"},
        headers=_auth(admin),
    )
    assert r3.status_code == 429, r3.text
    assert "已用完" in r3.json()["detail"]


def test_unknown_permission_key_rejected(env):
    """清单里不存在的 key 不能落库：下次生效时谁也校验不过。"""
    client = TestClient(app)
    admin = _admin_token(client)
    r = client.put(
        "/api/admin/role-permissions/member",
        json={"permissions": ["chat.send", "layout.launch_missile"]},
        headers=_auth(admin),
    )
    assert r.status_code == 400 and "权限点" in r.json()["detail"]

    r2 = client.put(
        "/api/admin/role-permissions/member",
        json={"quotas": {"chat": {"period": "day", "limit": 3}, "tts": {"limit": 1}}},
        headers=_auth(admin),
    )
    assert r2.status_code == 400 and "配额功能" in r2.json()["detail"]

    # 同一权限不能同时允许和禁止（隐式行为会在 UI 上显示成自相矛盾）
    uid = _register(client).json()["user"]["id"]
    r3 = client.put(
        f"/api/admin/users/{uid}/overrides",
        json={"perm_override": {"grant": ["chat.send"], "deny": ["chat.send"]}},
        headers=_auth(admin),
    )
    assert r3.status_code == 400 and "同时" in r3.json()["detail"]


# ================================================================ 配额内核
def test_quota_blocks_after_limit_with_reset_hint(env):
    """超限硬拦截，文案要带「已用 / 上限 / 什么时候重置」。"""
    client = TestClient(app)
    token = _register(client).json()["token"]
    uid = _me(client, token)["id"]
    _set_user_override(env.Maker, uid, quota_override={"chat": {"period": "day", "limit": 2}})

    asyncio.run(quota_mod.consume(uid, "chat", {"period": "day", "limit": 2}))
    asyncio.run(quota_mod.consume(uid, "chat", {"period": "day", "limit": 2}))
    assert _used(_me(client, token), "chat") == 2

    r = client.post("/api/chat/stream", json={"message": "还在吗"}, headers=_auth(token))
    assert r.status_code == 429, r.text
    detail = r.json()["detail"]
    assert "每日AI 对话" in detail or "每日AI对话" in detail or "每日" in detail
    assert "2/2" in detail and "重置" in detail


def test_quota_missing_or_zero_means_unlimited(env):
    """limit 缺失 / 0 / 负数 = **不限**。绝不能把「没配」当成 0 次。"""
    uid = asyncio.run(_add_user(env.Maker, "alice"))
    for conf in (None, {}, {"period": "day"}, {"period": "day", "limit": 0},
                 {"period": "day", "limit": -5}, {"period": "day", "limit": None}):
        for _ in range(3):
            asyncio.run(quota_mod.consume(uid, "video", conf))
    me = {"usage": asyncio.run(quota_mod.usage_of(uid, {}))}
    assert me["usage"]["video"]["unlimited"] is True
    assert me["usage"]["video"]["left"] is None


def test_day_and_month_periods_are_independent(env):
    """同一功能的日配额和月配额各记各的，不互相顶掉。"""
    uid = asyncio.run(_add_user(env.Maker, "bob"))
    day_conf = {"period": "day", "limit": 5}
    month_conf = {"period": "month", "limit": 100}
    for _ in range(3):
        asyncio.run(quota_mod.consume(uid, "layout", day_conf))
    for _ in range(7):
        asyncio.run(quota_mod.consume(uid, "layout", month_conf))

    snap = asyncio.run(quota_mod.usage_of(uid, {"layout": day_conf}))
    assert snap["layout"]["used"] == 3 and snap["layout"]["period"] == "day"
    snap2 = asyncio.run(quota_mod.usage_of(uid, {"layout": month_conf}))
    assert snap2["layout"]["used"] == 7 and snap2["layout"]["period"] == "month"


def test_quota_is_isolated_per_user(env):
    """配额按用户隔离：a 用完不影响 b。"""
    a = asyncio.run(_add_user(env.Maker, "alice"))
    b = asyncio.run(_add_user(env.Maker, "bobby"))
    conf = {"period": "day", "limit": 2}

    # a 用满，b 只用一半
    for _ in range(2):
        asyncio.run(quota_mod.consume(a, "voice_clone", conf))
    asyncio.run(quota_mod.consume(b, "voice_clone", conf))

    # a 已经用完 → 429
    with pytest.raises(Exception) as exc:
        asyncio.run(quota_mod.consume(a, "voice_clone", conf))
    assert getattr(exc.value, "status_code", None) == 429

    # b 还有余额 → 照常通过（a 的额度不会漏到 b 头上，也不会互相顶掉）
    asyncio.run(quota_mod.consume(b, "voice_clone", conf))


def test_concurrent_consume_never_exceeds_limit(env):
    """并发下也不能超发。

    读-改-写会丢更新（两个请求都读到 4、都写成 5，实际扣了 2 次只记 1 次），
    所以计数走单语句条件 UPDATE。这条就是锁那个实现的。
    """
    uid = asyncio.run(_add_user(env.Maker, "carol"))
    limit = 5
    conf = {"period": "day", "limit": limit}

    async def _hammer():
        results = await asyncio.gather(
            *(quota_mod.consume(uid, "video", conf) for _ in range(limit * 4)),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, Exception)]
        bad = [r for r in results if isinstance(r, Exception)]
        return ok, bad

    ok, bad = asyncio.run(_hammer())
    used = asyncio.run(quota_mod.usage_of(uid, {"video": conf}))["video"]["used"]
    assert used == limit, f"超发了：实际扣了 {used} 次，上限 {limit}"
    assert len(ok) == limit and len(bad) == limit * 4 - limit
    assert all(getattr(e, "status_code", None) == 429 for e in bad)


def test_quota_reset_clears_counts_only(env):
    """人工重置只清计数，不清配置：下个周期照样从 0 开始计。"""
    client = TestClient(app)
    admin = _admin_token(client)
    bob = _register(client, "bob").json()
    uid = bob["user"]["id"]

    r = client.put(
        f"/api/admin/users/{uid}/overrides",
        json={"quota_override": {"layout": {"period": "day", "limit": 2}}},
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    assert r.json()["quotas"]["layout"] == {"period": "day", "limit": 2}

    asyncio.run(quota_mod.consume(uid, "layout", {"period": "day", "limit": 2}))
    assert _used(_me(client, bob["token"]), "layout") == 1

    # 重置后计数归零，但配额配置还在
    r2 = client.post(f"/api/admin/users/{uid}/quota-reset", headers=_auth(admin))
    assert r2.status_code == 200, r2.text
    assert r2.json()["usage"]["layout"]["used"] == 0
    assert r2.json()["quotas"]["layout"]["limit"] == 2
    assert _used(_me(client, bob["token"]), "layout") == 0

    # 只重置单个功能
    asyncio.run(quota_mod.consume(uid, "layout", {"period": "day", "limit": 2}))
    asyncio.run(quota_mod.consume(uid, "chat", {"period": "day", "limit": 2}))
    r3 = client.post(
        f"/api/admin/users/{uid}/quota-reset", json={"feature": "layout"}, headers=_auth(admin)
    )
    assert r3.json()["usage"]["layout"]["used"] == 0
    assert r3.json()["usage"]["chat"]["used"] == 1

    # 不支持的功能名要拒
    assert client.post(
        f"/api/admin/users/{uid}/quota-reset", json={"feature": "tts"}, headers=_auth(admin)
    ).status_code == 400


# ================================================================ 端点映射与退还
def test_confirm_maps_tool_name_to_its_own_permission(env):
    """`/chat/confirm` 按 tool_name 映射权限，不设统一的「确认」权限点。

    一个端点承载三个工具，设统一权限会造成「有 video.render 却点不了确认」
    的死锁。这里验证映射表本身和它在端点上的实际效果。
    """
    assert TOOL_PERM["render_video"] == ("video.render", "video")
    assert TOOL_PERM["push_wechat_draft"] == ("wechat.push", "wechat_push")
    assert TOOL_PERM["save_content"] == ("queue.create", None)

    client = TestClient(app)
    admin = _admin_token(client)
    bob = _register(client, "bob").json()
    uid = bob["user"]["id"]

    # 不给 render_video 权限 → 确认它 403
    client.put(
        f"/api/admin/users/{uid}/overrides",
        json={"perm_override": {"grant": [], "deny": ["video.render"]}},
        headers=_auth(admin),
    )
    r = client.post(
        "/api/chat/confirm",
        json={"tool_call_id": "t1", "tool_name": "render_video", "arguments": {}},
        headers=_auth(bob["token"]),
    )
    assert r.status_code == 403, r.text

    # 同一个端点、同一个用户，换成有权限的工具：放行（执行体打桩）
    monkeypatched = {"result": {"ok": True, "media_id": "m1"}}
    original = chat_mod.execute_confirmed_tool

    async def _fake(db, ctx, tool_name, arguments):
        return monkeypatched["result"]

    chat_mod.execute_confirmed_tool = _fake
    try:
        r2 = client.post(
            "/api/chat/confirm",
            json={"tool_call_id": "t2", "tool_name": "push_wechat_draft", "arguments": {}},
            headers=_auth(bob["token"]),
        )
        assert r2.status_code == 200, r2.text
        # 成功 → 配额记一次，不退还
        assert _used(_me(client, bob["token"]), "wechat_push") == 1
    finally:
        chat_mod.execute_confirmed_tool = original


def test_push_failure_refunds_quota(env):
    """推送草稿是唯一「同步可判成败」的受控动作，失败必须退还。"""
    client = TestClient(app)
    bob = _register(client, "bob").json()

    original = chat_mod.execute_confirmed_tool

    async def _fake(db, ctx, tool_name, arguments):
        return {"error": "access_token 无效"}

    chat_mod.execute_confirmed_tool = _fake
    try:
        r = client.post(
            "/api/chat/confirm",
            json={"tool_call_id": "t1", "tool_name": "push_wechat_draft", "arguments": {}},
            headers=_auth(bob["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["result"]["error"]
    finally:
        chat_mod.execute_confirmed_tool = original

    # 失败了但次数没被吃掉 —— 否则失败一次就白扣一次额度
    assert _used(_me(client, bob["token"]), "wechat_push") == 0


def test_me_reports_permissions_quotas_usage(env):
    """/me 是权限与用量的权威快照：管理员改完，下一个 /me 就能看到。"""
    client = TestClient(app)
    admin = _admin_token(client)
    bob = _register(client, "bob").json()
    uid = bob["user"]["id"]

    client.put(
        f"/api/admin/users/{uid}/overrides",
        json={
            "perm_override": {"grant": ["settings.wechat_edit"], "deny": ["video.render"]},
            "quota_override": {"video": {"period": "month", "limit": 3}},
        },
        headers=_auth(admin),
    )

    me = _me(client, bob["token"])
    assert "settings.wechat_edit" in me["permissions"]
    assert "video.render" not in me["permissions"]
    assert me["quotas"]["video"] == {"period": "month", "limit": 3}
    snap = me["usage"]["video"]
    assert snap["limit"] == 3 and snap["used"] == 0 and snap["left"] == 3
    assert snap["period"] == "month" and snap["unlimited"] is False
    assert snap["reset_at"] > 0
    # 没被覆盖的功能仍然不限
    assert me["usage"]["chat"]["unlimited"] is True


def test_permission_catalog_shape(env):
    """权限目录给前端渲染勾选矩阵：分组、key、显示名、周期都要有。"""
    client = TestClient(app)
    admin = _admin_token(client)
    body = client.get("/api/admin/permissions", headers=_auth(admin)).json()

    keys = {p["key"] for p in body["permissions"]}
    assert keys == set(PERMISSION_KEYS)
    assert all({"group", "key", "label"} <= set(p) for p in body["permissions"])
    assert {f["key"] for f in body["quota_features"]} == set(quota_mod.FEATURES)
    assert {p["key"] for p in body["periods"]} == {"day", "month"}

    # admin 固定全开（permissions 为 null），member 默认全权限（含菜单级）
    assert body["roles"]["admin"]["permissions"] is None
    assert body["roles"]["admin"]["locked"] is True
    assert set(body["roles"]["member"]["permissions"]) == set(PERMISSION_KEYS)

    # member 越权访问这个管理端点 → 403
    member = _register(client, "bob").json()["token"]
    assert client.get("/api/admin/permissions", headers=_auth(member)).status_code == 403


def test_role_template_put_round_trip(env):
    """角色模板写入后读回来一致，且立即影响该角色的所有人。"""
    client = TestClient(app)
    admin = _admin_token(client)
    bob = _register(client, "bob").json()

    r = client.put(
        "/api/admin/role-permissions/member",
        json={
            "permissions": ["menu.chat", "chat.send", "layout.run"],
            "quotas": {"layout": {"period": "day", "limit": 4}, "chat": {"limit": 0}},
        },
        headers=_auth(admin),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["permissions"]) == {"menu.chat", "chat.send", "layout.run"}
    # limit=0 → 归一成「不限」
    assert r.json()["quotas"]["chat"]["limit"] is None
    assert r.json()["quotas"]["layout"] == {"period": "day", "limit": 4}

    me = _me(client, bob["token"])
    assert set(me["permissions"]) == {"menu.chat", "chat.send", "layout.run"}
    assert me["usage"]["layout"]["limit"] == 4
    assert me["usage"]["chat"]["unlimited"] is True

    # 只传配额不动权限
    r2 = client.put(
        "/api/admin/role-permissions/member",
        json={"quotas": {"video": {"period": "month", "limit": 2}}},
        headers=_auth(admin),
    )
    assert set(r2.json()["permissions"]) == {"menu.chat", "chat.send", "layout.run"}
    assert r2.json()["quotas"]["video"] == {"period": "month", "limit": 2}

    # 空 body 拒绝
    assert client.put(
        "/api/admin/role-permissions/member", json={}, headers=_auth(admin)
    ).status_code == 400
    # 非法角色名拒绝
    assert client.put(
        "/api/admin/role-permissions/root", json={"quotas": {}}, headers=_auth(admin)
    ).status_code == 400


def test_admin_list_includes_permissions_and_usage(env):
    """用户列表一次带全：权限、配额、本周期用量（管理页要靠它们渲染）。"""
    client = TestClient(app)
    admin = _admin_token(client)
    bob = _register(client, "bob").json()
    uid = bob["user"]["id"]

    asyncio.run(quota_mod.consume(uid, "chat", None))  # 不限也计数
    rows = {u["username"]: u for u in client.get("/api/admin/users", headers=_auth(admin)).json()}
    assert set(rows) == {"admin", "bob"}
    assert rows["bob"]["usage"]["chat"]["used"] == 1
    assert "chat.send" in rows["bob"]["permissions"]
    # admin 那一行也是全权限
    assert set(rows["admin"]["permissions"]) == set(PERMISSION_KEYS)
