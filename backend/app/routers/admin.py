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
"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.core.rbac import ROLES, ROLE_ADMIN, ROLE_MEMBER, count_admins, require_admin
from app.core.security import hash_password
from app.models.content import Content, QueueItem
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


class CreateUserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: str = ROLE_MEMBER


class UpdateUserIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None


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


def _user_row(user: User, contents: int = 0) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        created_at=user.created_at,
        role=user.role,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        contents=contents,
    )


async def _content_counts(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(select(Content.owner, func.count()).group_by(Content.owner))).all()
    return {owner: n for owner, n in rows}


# ---------------------------------------------------------------- 端点
@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    operator: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """全部用户。含内容数，一眼看出谁是空账号、谁在用。"""
    users = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    counts = await _content_counts(db)
    return [_user_row(u, counts.get(u.id, 0)) for u in users]


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
    return _user_row(user)


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
    return _user_row(target, counts.get(target.id, 0))


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
