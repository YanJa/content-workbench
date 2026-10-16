"""角色与权限（RBAC）。

设计口径
--------
两级角色，够用就停：

- `admin`：能管账号（建号 / 改角色 / 停用）+ 看全站聚合统计；
- `member`：只能用业务功能，管自己的数据。

**管理员看不到别人的内容。** 所有业务端点仍走 owner 隔离（见
core/tenant.py），admin 也只是「自己那个 owner」。给管理员开跨用户读取
等于先制造风险再防御风险，不划算 —— 需要协同的场景应该靠分享链接，
而不是给一个账号开全库视野。

管理员从哪来
------------
**系统预置，不靠注册顺序。** 启动时 `ensure_admin_account()` 保证
`settings.ADMIN_USERNAME`（默认 `admin`）这个账号存在且是 admin：

- 账号已存在（比如你先自己注册了一个同名的）→ 只把角色摆正，**不动密码**；
- 账号不存在 → 建一个，初始密码取 `settings.ADMIN_INITIAL_PASSWORD`；
  没配置就随机生成并打进启动日志。

刻意不做「第一个注册的人自动当管理员」：那是把权限交给一个时间顺序，
而不是交给一个明确的账号 —— 谁先打开页面谁就是管理员，这规则在演示时
很方便，在真机上很危险（先来的是路人甲也一样拿到管理员）。

三个反直觉的约定（都是踩过或推演过的坑）
--------------------------------------
1. **role 不进 JWT。** JWT 有效期 7 天，把角色签进去意味着改一次角色要
   等最多 7 天才生效（或者强制所有人重登）。每次请求查库多一次主键查询，
   换来「改完立即生效」，值。
2. **停用（is_active）也不进 JWT**，同理。而且停用必须立刻生效 ——
   一个被踢出团队的人不该还能用旧 token 继续干活 7 天。
3. **查「有没有 admin」时不能带 is_active 过滤。** 停用 ≠ 撤权。如果带
   过滤，把所有 admin 停一次再重启，系统会认为「没有管理员了」于是另立
   一个 —— 停用人反而成了提权手段。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy import func, select

from app.core.logging import logger
from app.core.tenant import current_user
from app.db.session import SessionLocal
from app.models.user import User

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MEMBER)


async def require_admin(user: User = Depends(current_user)) -> User:
    """FastAPI 依赖：admin 专属端点。

    返回 User 而不是 None —— 端点需要拿操作者做「不能对自己动手」的校验
    （见 routers/admin.py 的 `_guard`）。
    """
    if user.role != ROLE_ADMIN:
        raise HTTPException(403, "需要管理员权限")
    return user


async def ensure_admin_account() -> None:
    """启动时预置管理员账号。幂等，可反复调用。

    为什么是自愈而不是一次性初始化脚本：脚本只在它被执行过一次的那台机器上
    成立。换库、重装、从备份恢复之后还得记得再跑一次 —— 会忘。放在
    lifespan 里，每次启动都自动摆正。

    两种结果：

    - 同名账号已存在 → 只把 `role` 摆正，**绝不改密码**（那可能是用户自己
      设的密码，覆盖掉等于把人锁在门外）；
    - 不存在 → 建一个，初始密码取 `settings.ADMIN_INITIAL_PASSWORD`；
      没配就随机生成并打进启动日志（只在创建这一次打，不会每次刷屏）。
    """
    from app.core.config import settings
    from app.core.security import hash_password

    username = (settings.ADMIN_USERNAME or "admin").strip() or "admin"

    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()

        if user is not None:
            if user.role != ROLE_ADMIN:
                user.role = ROLE_ADMIN
                await db.commit()
                logger.warning("已把账号 %s 的角色摆正为 admin", username)
            return

        raw = (settings.ADMIN_INITIAL_PASSWORD or "").strip()
        generated = not raw
        if generated:
            import secrets

            raw = secrets.token_urlsafe(9)

        db.add(User(username=username, password_hash=hash_password(raw), role=ROLE_ADMIN))
        await db.commit()
        logger.warning(
            "已预置管理员账号 %s；初始密码：%s%s",
            username,
            raw,
            "（随机生成，请登录后立即修改）" if generated else "",
        )


async def count_admins(db) -> int:
    """当前 admin 人数。同样**不带 is_active 过滤**（停用不撤权）。"""
    return int(
        (
            await db.execute(
                select(func.count()).select_from(User).where(User.role == ROLE_ADMIN)
            )
        ).scalar_one()
    )
