"""管理面路由：用户管理 + 全站聚合统计。

边界（重要）
------------
管理员**只能管账号和看聚合数字**，看不到别人的内容。所有业务端点仍然走
owner 隔离（`core/tenant.py`），admin 登录后也只是「他自己那个 owner」。
要协同就走分享链接，不给任何账号开全库视野。

为什么没有 DELETE
-----------------
删除 = 停用（`is_active=False`），不做真删：

- 存量直链签名绑 `?o=<uid>`，删了用户他的历史配图会集体 404；
- 为腾出 username 唯一索引而改名，会让「恢复」变成只能手工改库；
- 真删不可逆，而「停用」随时能一键恢复。

想彻底清除某个用户及其数据的话，得连带处理 contents.images 里的签名 URL
和 data/voices/owners/<uid>/ 目录 —— 那是另一个功能，本轮不做。

权限与配额管理（按钮级权限，2026-10 上线）
----------------------------------------
本文件同时是细粒度权限的管理面，四个端点：

- `GET /admin/permissions` —— 权限点目录 + 各角色当前模板（前端渲染勾选矩阵）；
- `PUT /admin/role-permissions/{role}` —— 改某个角色的权限清单与配额；
- `PUT /admin/users/{id}/overrides` —— 给单个用户开/关权限点、改配额；
- `POST /admin/users/{id}/quota-reset` —— 人工重置某人的用量计数。

三条口径（与 core/rbac.py 一致，这里负责执行）：

1. **admin 的权限不可收窄** —— 管理员把自己勾成「什么都没有」是最常见的
   自锁方式，接口层直接 400。但 admin 的**配额照走**：配额是成本闸门不是
   权限，管理页给 admin 配的额度必须真的生效，否则 UI 在说谎。
2. **角色模板只覆盖不冻结**：用户没被单独覆盖时，永远跟着角色模板走
   （改模板立即影响一批人）；被覆盖过的是「增量」而不是「快照」，
   不会把用户钉死在管理员改他那一刻的状态上。
3. **空 / 0 / 负数 = 不限**，绝不等于 0 次。
"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import quota as quota_mod
from app.core import invite as invite_mod
from app.core.logging import logger
from app.core.rbac import (
    DEFAULT_PERMISSIONS,
    PERMISSIONS,
    PERMISSION_KEYS,
    QUOTA_FEATURES,
    ROLES,
    ROLE_ADMIN,
    ROLE_MEMBER,
    count_admins,
    effective_perms,
    effective_quotas,
    require_admin,
)
from app.core.security import hash_password
from app.models.content import Content, QueueItem
from app.models.invite import InviteCode
from app.models.permission import RolePermission
from app.models.renderjob import RenderJob
from app.models.usage import LlmUsage
from app.models.user import User
from app.db.session import get_db
from app.routers.auth import UserOut, validate_username
from app.services.usage import cost_of

router = APIRouter(
    prefix="/admin",
    tags=["管理"],
    # router 级依赖是硬保证：以后往这个文件里加端点忘写校验也拦得住。
    # 各端点仍显式声明 `operator: User = Depends(require_admin)` —— 一是要拿
    # 操作者做防自锁校验，二是让「端点鉴权回归」测试能在源码里看到依赖名。
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------- 模型
class AdminUserOut(UserOut):
    """用户列表项：在 UserOut 之上多带一个内容数，方便看谁在用。"""

    last_login_at: int | None = None
    contents: int = 0
    # 来源邀请码的渠道备注（如「小红书投放」）。空 = 无来源/码已删。
    invite_note: str | None = None


class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: str = ROLE_MEMBER


class UpdateUserIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class RolePermIn(BaseModel):
    """角色模板修改。两个字段都可选（只传配额就只改配额），但不能都空。"""

    permissions: list[str] | None = None
    quotas: dict | None = None


class UserOverrideIn(BaseModel):
    """单个用户的权限/配额覆盖。增量语义，不是快照（见端点说明）。"""

    perm_override: dict | None = None
    quota_override: dict | None = None


class QuotaResetIn(BaseModel):
    feature: str | None = None


# ---------------------------------------------------------------- 防自锁
async def _guard(
    db: AsyncSession,
    target: User,
    operator: User,
    *,
    new_role: str | None = None,
    deactivate: bool = False,
) -> None:
    """管理员动手前的三条硬校验。顺序即优先级，先快后慢。"""
    if target.id == operator.id:
        # 管理员把自己降成 member、或者停掉自己，都是在给系统制造孤儿
        raise HTTPException(400, "不能对自己的账号执行此操作")

    if new_role is not None and new_role not in ROLES:
        # 非法值存进库里的话，下次启动 ensure_admin_exists() 找不到 admin
        # 会去升另一个人，等于凭空换了个管理员
        raise HTTPException(400, f"非法的角色值：{new_role}（可选：{', '.join(ROLES)}）")

    losing_admin = target.role == ROLE_ADMIN and (
        (new_role is not None and new_role != ROLE_ADMIN) or deactivate
    )
    if losing_admin and await count_admins(db) <= 1:
        raise HTTPException(400, "至少需要保留一名管理员")


async def _role_row(db: AsyncSession, role: str) -> RolePermission | None:
    return (
        await db.execute(select(RolePermission).where(RolePermission.role == role))
    ).scalar_one_or_none()


def _clean_perm_keys(keys) -> list[str]:
    """权限点清单清洗：必须是字符串数组、必须是清单里的 key、去重且按目录顺序。

    顺序固定很重要：同样一份勾选，存进去和读出来长得一样，前端 diff 才不会
    每次都觉得「改了」。
    """
    if not isinstance(keys, (list, tuple)):
        raise HTTPException(400, "权限点必须是字符串数组")
    bad = [k for k in keys if k not in PERMISSION_KEYS]
    if bad:
        raise HTTPException(400, f"未知的权限点：{', '.join(map(str, bad))}")
    picked = set(keys)
    return [k for k in PERMISSION_KEYS if k in picked]


def _clean_quotas(raw, *, strict: bool = True) -> dict:
    """配额配置归一：只认 QUOTA_FEATURES 里的功能，limit 归一到 int|None。

    - `limit=None` / 0 / 负数 → 不限（`core.quota.normalize` 的口径）；
    - `strict=False` 用在**读**路径：库里可能有手改过的脏数据，跳过而不是 500；
      `strict=True` 用在**写**路径：管理员配错了要当场告诉他，不能静默吞掉。
    """
    if not isinstance(raw, dict):
        if strict:
            raise HTTPException(400, "配额配置格式不正确，应为 {功能: {period, limit}}")
        return {}
    out: dict = {}
    for feature, conf in raw.items():
        if feature not in QUOTA_FEATURES:
            if strict:
                raise HTTPException(
                    400, f"不支持的配额功能：{feature}（可选：{', '.join(QUOTA_FEATURES)}）"
                )
            continue
        limit, period = quota_mod.normalize(conf if isinstance(conf, dict) else None)
        out[feature] = {"period": period, "limit": limit}
    return out


def _clean_override(raw) -> dict:
    """用户级权限覆盖清洗：`{"grant": [...], "deny": [...]}`。

    同一个权限点不能同时出现在两边。实现上 deny 赢（后减），但那是隐式行为，
    UI 上会显示成「既允许又禁止」—— 直接拒掉，逼管理员想清楚。
    """
    if not isinstance(raw, dict):
        raise HTTPException(400, "权限覆盖格式不正确，应为 {grant: [], deny: []}")
    grant = _clean_perm_keys(raw.get("grant") or [])
    deny = _clean_perm_keys(raw.get("deny") or [])
    both = sorted(set(grant) & set(deny))
    if both:
        raise HTTPException(400, f"同一权限不能同时允许和禁止：{', '.join(both)}")
    return {"grant": grant, "deny": deny}


async def _user_row(
    user: User,
    db: AsyncSession,
    contents: int = 0,
    invite_notes: dict[str, str] | None = None,
) -> AdminUserOut:
    """用户列表项 + 生效的权限/配额/用量快照。

    一并算出来是因为管理页要用：改完权限想立刻看到这个人现在到底能干什么，
    还得再挨个端点问一遍，不如一次给全。`invite_notes` 是「码 → 渠道备注」
    的预取映射（list_users 一次性查好，避免每个用户各查一次邀请码表）。
    """
    row = await _role_row(db, user.role)
    quotas = effective_quotas(user, row)
    note = None
    if user.invite_code and invite_notes is not None:
        note = invite_notes.get(user.invite_code)
    return AdminUserOut(
        id=user.id,
        username=user.username,
        created_at=user.created_at,
        role=user.role,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        contents=contents,
        invite_code=user.invite_code,
        invite_note=note,
        permissions=effective_perms(user, row),
        quotas=quotas,
        usage=await quota_mod.usage_of(user.id, quotas, db=db),
    )


async def _content_counts(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(select(Content.owner, func.count()).group_by(Content.owner))).all()
    return {owner: n for owner, n in rows}


async def _invite_notes(db: AsyncSession) -> dict[str, str]:
    """码 → 渠道备注。列表页预取，避免 N+1。"""
    rows = (await db.execute(select(InviteCode.code, InviteCode.note))).all()
    return {code: note for code, note in rows}


# ---------------------------------------------------------------- 端点
@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """全部用户。含内容数，一眼看出谁是空账号、谁在用。"""
    users = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    counts = await _content_counts(db)
    notes = await _invite_notes(db)
    return [await _user_row(u, db, counts.get(u.id, 0), notes) for u in users]


@router.post("/users", response_model=AdminUserOut)
async def create_user(
    req: CreateUserIn,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员代建账号 —— 绕过邀请码（邀请码是给自助注册用的门槛）。

    建出来一律是启用状态；想建完就封，再调一次 PATCH。
    """
    username = validate_username(req.username)
    if len(req.password) < 8:
        raise HTTPException(422, "密码至少 8 位")
    if req.role not in ROLES:
        raise HTTPException(400, f"非法的角色值：{req.role}（可选：{', '.join(ROLES)}）")

    taken = (await db.execute(select(User.id).where(User.username == username))).first()
    if taken:
        raise HTTPException(409, "用户名已被占用")

    user = User(username=username, password_hash=hash_password(req.password), role=req.role)
    db.add(user)
    await db.commit()
    logger.info("管理员 %s 创建用户 %s (role=%s)", operator.username, username, req.role)
    return await _user_row(user, db)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: str,
    req: UpdateUserIn,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """改角色 / 停用 / 恢复启用。

    停用即时生效：角色和停用标记都不进 JWT（见 core/rbac.py），被停用的人
    手里那张还没过期的 token 下一次请求就 401，不用等 7 天。
    """
    if req.role is None and req.is_active is None:
        raise HTTPException(400, "没有要修改的字段")

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "用户不存在")

    await _guard(
        db,
        target,
        operator,
        new_role=req.role,
        deactivate=req.is_active is False,
    )

    if req.role is not None:
        target.role = req.role
    if req.is_active is not None:
        target.is_active = req.is_active
    await db.commit()

    logger.warning(
        "管理员 %s 修改用户 %s：role=%s is_active=%s",
        operator.username,
        target.username,
        target.role,
        target.is_active,
    )
    counts = await _content_counts(db)
    return await _user_row(target, db, counts.get(target.id, 0))


@router.get("/stats")
async def stats(
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """全站聚合：只报数字，不报内容。

    刻意**不复用** `services.usage.summary(db, owner=...)`：那个函数内部有
    `owner or "default"` 兜底，传 None 会去统计 'default' 租户，得不到全站。
    """
    users = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    counts = await _content_counts(db)

    queue_count = (await db.execute(select(func.count()).select_from(QueueItem))).scalar_one()
    render_count = (await db.execute(select(func.count()).select_from(RenderJob))).scalar_one()

    # 用量：一次取全表在内存里按 owner 切。表按次调用一行，量级可控；
    # 真涨到几十万行再换 SQL 分组聚合。
    usage_rows = (await db.execute(select(LlmUsage))).scalars().all()
    cost_by_owner: dict[str, float] = defaultdict(float)
    calls_by_owner: dict[str, int] = defaultdict(int)
    tokens_by_owner: dict[str, int] = defaultdict(int)
    for r in usage_rows:
        cost_by_owner[r.owner] += cost_of(r)
        calls_by_owner[r.owner] += 1
        tokens_by_owner[r.owner] += r.total_tokens

    by_user = [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "contents": counts.get(u.id, 0),
            "llm_calls": calls_by_owner.get(u.id, 0),
            "total_tokens": tokens_by_owner.get(u.id, 0),
            "cost": round(cost_by_owner.get(u.id, 0.0), 6),
        }
        for u in users
    ]

    return {
        "users": {
            "total": len(users),
            "active": sum(1 for u in users if u.is_active),
            "admins": sum(1 for u in users if u.role == ROLE_ADMIN),
        },
        "contents": sum(counts.values()),
        "queue_items": int(queue_count),
        "render_jobs": int(render_count),
        "llm": {
            "calls": len(usage_rows),
            "total_tokens": sum(r.total_tokens for r in usage_rows),
            "cost": round(sum(cost_by_owner.values()), 6),
        },
        "by_user": by_user,
    }


# ---------------------------------------------------------------- 权限与配额
async def _role_snapshot(db: AsyncSession, role: str) -> dict:
    """某个角色的当前模板快照（权限清单 + 配额）。

    admin 的 `permissions` 返回 **None**，语义是「全部开放且固定」——
    前端据此把勾选框渲染成禁用态。返回 None 而不是返回全量 key 列表，
    是为了让「固定」和「恰好全勾」在数据上可区分。
    """
    row = await _role_row(db, role)
    locked = role == ROLE_ADMIN
    if locked:
        perms: list[str] | None = None
    elif row is not None and row.permissions is not None:
        # 表里有配置 → 以表为准（空列表 = 明确的「什么都没有」）
        perms = [k for k in PERMISSION_KEYS if k in set(row.permissions)]
    else:
        # 没配过 → 代码默认。这就是「表是覆盖层，不是唯一真源」
        perms = list(DEFAULT_PERMISSIONS.get(ROLE_MEMBER, ()))
    return {
        "permissions": perms,
        "locked": locked,
        "quotas": _clean_quotas(row.quotas if row else None, strict=False),
    }


@router.get("/permissions")
async def permission_catalog(
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """权限点目录 + 各角色当前模板。管理页的「权限模板」卡据此渲染。

    目录来自代码常量（`core/rbac.py` 的 PERMISSIONS），**不从库里读** ——
    库里只有「改过没有」，清单永远以代码为准；否则手改库造出的脏 key 会
    渲染出一堆前端不认识的勾选框。
    """
    return {
        "permissions": [{"group": g, "key": k, "label": label} for g, k, label in PERMISSIONS],
        "quota_features": [
            {"key": f, "label": quota_mod.FEATURES[f]} for f in QUOTA_FEATURES
        ],
        "periods": [
            {"key": p, "label": quota_mod.PERIOD_LABEL[p]} for p in quota_mod.PERIODS
        ],
        "roles": {
            ROLE_MEMBER: await _role_snapshot(db, ROLE_MEMBER),
            ROLE_ADMIN: await _role_snapshot(db, ROLE_ADMIN),
        },
    }


@router.put("/role-permissions/{role}")
async def put_role_permissions(
    role: str,
    req: RolePermIn,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """改某个角色的权限清单 / 配额。

    - member 可增可减；
    - **admin 只能改配额**，权限清单碰不得：管理员把自己勾成「什么都没有」
      是最常见的自锁方式，接口层直接 400。但配额照走 —— 那是成本闸门，
      给 admin 配的额度必须真的生效，否则管理页在说谎。

    没配过的角色第一次写入才建行；已存在的行是**覆盖**语义，不影响别人。
    """
    if role not in ROLES:
        raise HTTPException(400, f"非法的角色值：{role}（可选：{', '.join(ROLES)}）")
    if req.permissions is None and req.quotas is None:
        raise HTTPException(400, "没有要修改的内容")

    row = await _role_row(db, role)

    if req.permissions is not None:
        if role == ROLE_ADMIN:
            raise HTTPException(400, "管理员权限固定为全部开放，不能收窄（防止把自己锁在门外）")
        if row is None:
            row = RolePermission(role=role)
            db.add(row)
        row.permissions = _clean_perm_keys(req.permissions)

    if req.quotas is not None:
        if row is None:
            row = RolePermission(role=role)
            db.add(row)
        row.quotas = _clean_quotas(req.quotas)

    await db.commit()
    logger.warning(
        "管理员 %s 修改角色模板 %s：permissions=%s quotas=%s",
        operator.username,
        role,
        req.permissions is not None,
        sorted(req.quotas or {}),
    )
    return await _role_snapshot(db, role)


@router.put("/users/{user_id}/overrides")
async def put_user_overrides(
    user_id: str,
    req: UserOverrideIn,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """给单个用户开/关权限点、改配额（角色默认之上的增量覆盖）。

    **相对角色模板是增量，不是快照**：`grant` 只加不减，之后角色模板新增的
    功能这个人照样能收到；快照会把他钉死在管理员改他那一刻的状态上，模板
    升级传不下去，而且没人想得到是因为这个。

    注意**覆盖块本身是全量替换**：一次 PUT 换掉整个 grant/deny。所以前端
    的保存姿势是「按有效权限反算差集」—— 把勾选矩阵整体提交，而不是往里
    追加一项。这也正是 admin 例外存在的原因：见下。

    同样不对 admin 生效（管理员权限固定全开）—— 但 admin 的**配额**可以
    单独覆盖，成本闸门对所有人一视同仁。
    """
    if req.perm_override is None and req.quota_override is None:
        raise HTTPException(400, "没有要修改的内容")

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "用户不存在")

    if req.perm_override is not None:
        if target.role == ROLE_ADMIN:
            raise HTTPException(400, "管理员权限固定为全部开放，无需单独覆盖")
        target.perm_override = _clean_override(req.perm_override)
    if req.quota_override is not None:
        target.quota_override = _clean_quotas(req.quota_override)

    await db.commit()
    logger.warning(
        "管理员 %s 修改用户 %s 的覆盖：perm=%s quota=%s",
        operator.username,
        target.username,
        req.perm_override is not None,
        sorted(req.quota_override or {}),
    )
    return await _user_row(target, db)


@router.post("/users/{user_id}/quota-reset")
async def reset_user_quota(
    user_id: str,
    req: QuotaResetIn | None = None,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """人工重置某人的用量计数（比如他误操作把今天/本月的额度耗尽了）。

    只清**计数**，不清配置：上限该是多少仍按角色模板/个人覆盖算，
    重置完下个周期照样从 0 开始计。`feature` 缺省 = 全部功能。

    body 可省略（`{}` 或不传都行）：清全部是最常用的操作，不值得为此
    逼前端构造一个空 JSON。
    """
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "用户不存在")

    feature = ((req.feature if req else None) or "").strip() or None
    if feature is not None and feature not in QUOTA_FEATURES:
        raise HTTPException(
            400, f"不支持的配额功能：{feature}（可选：{', '.join(QUOTA_FEATURES)}）"
        )

    # 先收尾本会话的事务：reset 走的是它自己的 session，而 SQLite 的读事务
    # 带快照 —— 不先提交的话，下面 _user_row 读到的可能还是重置前的旧计数，
    # 接口返回「重置完了但用量没变」，管理页会以为重置失败。
    await db.commit()
    await quota_mod.reset(target.id, feature)
    return await _user_row(target, db)


# ---------------------------------------------------------------- 邀请码管理
class InviteCodeOut(BaseModel):
    """邀请码列表项。含已用/上限，一眼看出哪个渠道还剩多少额度。"""

    id: str
    code: str
    note: str = ""
    max_uses: int | None = None
    used: int = 0
    disabled: bool = False
    created_at: int
    # 用尽 = used >= max_uses（max_uses 非空时）。前端据此显示「已用完」。
    exhausted: bool = False


class InviteCodeIn(BaseModel):
    code: str | None = None
    note: str = ""
    max_uses: int | None = None


def _invite_out(inv: InviteCode) -> InviteCodeOut:
    exhausted = inv.max_uses is not None and inv.used >= inv.max_uses
    return InviteCodeOut(
        id=inv.id,
        code=inv.code,
        note=inv.note,
        max_uses=inv.max_uses,
        used=inv.used,
        disabled=inv.disabled,
        created_at=inv.created_at,
        exhausted=exhausted,
    )


@router.get("/invite-codes", response_model=list[InviteCodeOut])
async def list_invite_codes(
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """全部邀请码，按创建时间倒序（最新在前）。"""
    rows = (
        await db.execute(select(InviteCode).order_by(InviteCode.created_at.desc()))
    ).scalars().all()
    return [_invite_out(inv) for inv in rows]


@router.post("/invite-codes", response_model=InviteCodeOut)
async def create_invite_code(
    req: InviteCodeIn,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """生成一条邀请码。

    `code` 留空则随机；`max_uses` 留空/0/负数 = 不限次。生成后即可复制
    发给用户注册。撞唯一约束（自定义码已存在）返回 409。
    """
    # 用户指定了码就校验一下形态：太长的码抄起来费劲，也防注入到别处
    custom = (req.code or "").strip()
    if custom:
        if not (3 <= len(custom) <= 64) or any(ch.isspace() for ch in custom):
            raise HTTPException(400, "邀请码需为 3~64 个非空字符")

    inv = await invite_mod.create(
        code=custom or None,
        note=req.note,
        max_uses=req.max_uses,
        created_by=operator.id,
        db=db,
    )
    await db.commit()
    return _invite_out(inv)


@router.post("/invite-codes/{code_id}/disable", response_model=InviteCodeOut)
async def disable_invite_code(
    code_id: str,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """停用一条邀请码（渠道取消合作 / 作废）。停用后立即不能再注册。"""
    inv = (await db.execute(select(InviteCode).where(InviteCode.id == code_id))).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "邀请码不存在")
    inv.disabled = True
    await db.commit()
    return _invite_out(inv)


@router.post("/invite-codes/{code_id}/enable", response_model=InviteCodeOut)
async def enable_invite_code(
    code_id: str,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """重新启用一条被停用的邀请码。用尽的不在此列（用尽是 used 打满，不是
    停用位；要给它续额度请另建一条）。"""
    inv = (await db.execute(select(InviteCode).where(InviteCode.id == code_id))).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "邀请码不存在")
    inv.disabled = False
    await db.commit()
    return _invite_out(inv)


@router.delete("/invite-codes/{code_id}", status_code=204)
async def delete_invite_code(
    code_id: str,
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除一条邀请码。只删「从没被用过」的码（used == 0）—— 用过的码是
    渠道统计的一部分，删了会让历史用户「来源」变空，渠道归因就断了。"""
    inv = (await db.execute(select(InviteCode).where(InviteCode.id == code_id))).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "邀请码不存在")
    if inv.used > 0:
        raise HTTPException(400, "该邀请码已有注册记录，不能删除（渠道统计需要保留），可改为停用")
    await db.delete(inv)
    await db.commit()
