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
from app.models.permission import RolePermission
from app.models.user import User

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MEMBER)

# ---------------------------------------------------------------- 权限点清单
# 单一真源：后端校验与前端 UI 都从这里拿（前端经 /api/admin/permissions）。
# 格式：(分组, key, 显示名)。分组用于管理页分块展示。
#
# 刻意**没有** chat.tool_confirm 这种点：`/api/chat/confirm` 一个端点承载
# save_content / render_video / push_wechat_draft 三个工具，设独立的「确认」
# 权限会造成「有 video.render 却点不了确认」的死锁。改成按 tool_name 映射到
# 目标权限点（见 TOOL_PERM）。
PERMISSIONS: list[tuple[str, str, str]] = [
    # 菜单级：控制侧栏可见性
    ("menu", "menu.chat", "对话创作"),
    ("menu", "menu.queue", "发布队列"),
    ("menu", "menu.video", "视频生产"),
    ("menu", "menu.usage", "AI 用量"),
    ("menu", "menu.revenue", "收益看板"),
    ("menu", "menu.settings", "系统状态"),
    ("menu", "menu.knowledge", "知识库"),
    ("menu", "menu.skills", "技能包"),
    ("menu", "menu.assets", "素材库"),
    # 按钮级：控制具体动作
    ("chat", "chat.send", "发送消息"),
    ("queue", "queue.create", "新建内容"),
    ("queue", "queue.delete", "删除内容"),
    ("video", "video.render", "生成视频"),
    ("video", "video.ppt_parse", "解析 PPT"),
    ("layout", "layout.run", "一键排版"),
    ("layout", "layout.image_gen", "AI 生图（不计次）"),
    ("layout", "layout.cover", "生成封面（不计次）"),
    ("knowledge", "knowledge.create", "新增知识库素材"),
    ("knowledge", "knowledge.delete", "删除知识库素材"),
    ("skill", "skill.manage", "管理技能包（增删改启停）"),
    ("revenue", "revenue.alias", "管理广告位别名"),
    ("voice", "voice.clone", "声音复刻"),
    ("wechat", "wechat.push", "推送公众号草稿"),
    ("settings", "settings.wechat_edit", "公众号增删改"),
    # 素材库：上传不设权限点（跟「新建内容」一个量级），只有「从在线图源导入」
    # 和「删除」值得单独管 —— 前者受图源条款限速，后者不可逆。
    ("assets", "assets.import", "从在线图源导入素材"),
    ("assets", "assets.crawl", "按文章采集配图"),
    ("assets", "assets.delete", "删除素材"),
]

PERMISSION_KEYS = tuple(k for _, k, _ in PERMISSIONS)
# 非菜单的功能权限。菜单级（menu.*）只控制侧栏可见性，不计入这份「功能」集合。
_FUNCTION_KEYS = tuple(k for g, k, _ in PERMISSIONS if g != "menu")

# 工具名 → (权限点, 配额 feature)。用于 /api/chat/confirm 按 tool_name 映射。
TOOL_PERM: dict[str, tuple[str, str | None]] = {
    "render_video": ("video.render", "video"),
    "push_wechat_draft": ("wechat.push", "wechat_push"),
    "delete_wechat_draft": ("wechat.push", None),  # 删除不耗配额（推送才计次）
    "save_content": ("queue.create", None),
}

# 角色默认权限。**表是覆盖层，这份常量才是兜底** —— role_permissions 表为空
# 时（刚部署、还没人改过）member 必须有全部功能，否则一次部署锁死全站。
#
# member 默认给**全部**权限点（含 menu.*）：菜单可见性默认全开，管理员想收窄
# 再去模板里取消勾选。反过来做（默认不给 menu.*）会让「取消勾选」变成「默认
# 就没有」—— 侧栏对一个新成员直接是空的，那是 fail-closed。
DEFAULT_PERMISSIONS: dict[str, tuple[str, ...] | str] = {
    ROLE_MEMBER: PERMISSION_KEYS,
    ROLE_ADMIN: "ALL",  # 管理员永远全权限，不查表、不受勾选影响（防自锁）
}

# 受配额管控的功能（与 PERMISSIONS 是两张表：生图/封面只受权限，不计次）
QUOTA_FEATURES = ("chat", "video", "layout", "voice_clone", "wechat_push")


async def require_admin(user: User = Depends(current_user)) -> User:
    """FastAPI 依赖：admin 专属端点。

    返回 User 而不是 None —— 端点需要拿操作者做「不能对自己动手」的校验
    （见 routers/admin.py 的 `_guard`）。
    """
    if user.role != ROLE_ADMIN:
        raise HTTPException(403, "需要管理员权限")
    return user


# ---------------------------------------------------------------- 有效权限/配额
async def _role_row(db, role: str) -> RolePermission | None:
    return (await db.execute(select(RolePermission).where(RolePermission.role == role))).scalar_one_or_none()


def effective_perms(user: User, role_row: RolePermission | None = None) -> list[str]:
    """用户的有效权限点列表。

    admin 直接全开（不看表、不看覆盖）—— 管理员把自己勾成「没有权限」是
    最常见的自锁方式，这里堵死。member 走：
    `（代码默认 ∪ 角色模板 ∪ grant） − deny`
    """
    if user.role == ROLE_ADMIN:
        return list(PERMISSION_KEYS)

    base = set(DEFAULT_PERMISSIONS.get(ROLE_MEMBER, ()))
    if role_row is not None and role_row.permissions is not None:
        # 表里有配置 → 以表为准（管理员可以显式设成空，表示「什么都没有」）
        base = set(role_row.permissions)

    ov = user.perm_override or {}
    base |= set(ov.get("grant") or [])
    base -= set(ov.get("deny") or [])
    # 过滤掉清单里已经不存在的 key（改过常量后留下的脏数据）
    return [k for k in PERMISSION_KEYS if k in base]


def effective_quotas(user: User, role_row: RolePermission | None = None) -> dict:
    """用户的有效配额：角色模板 per-feature 覆盖上用户自己的覆盖。"""
    quotas: dict = dict((role_row.quotas or {}) if role_row is not None else {})
    quotas.update(user.quota_override or {})
    return {k: v for k, v in quotas.items() if k in QUOTA_FEATURES}


async def load_permissions(db, user: User) -> tuple[list[str], dict]:
    """一次查库拿到（权限, 配额）。给 /auth/me 与 require_use 用。"""
    row = await _role_row(db, user.role)
    return effective_perms(user, row), effective_quotas(user, row)


def has_perm(perms: list[str] | set[str], key: str) -> bool:
    return key in perms


# ---------------------------------------------------------------- 依赖工厂
def require_use(perm: str | None = None, quota: str | None = None):
    """FastAPI 依赖工厂：**权限校验 + 配额扣减**合成一个。

    埋点处这两件事必然成对出现，合成一个省得每个端点写两遍（也避免漏配额）。

    - `perm`：需要的功能权限点（admin 自动通过）；
    - `quota`：要扣的功能（**admin 也扣** —— 配额是成本闸门，不是权限，
      管理页给 admin 配的额度必须真的生效，否则 UI 在说谎）。

    FastAPI 按请求缓存依赖结果，所以和已有的 `current_owner_id` 并存时
    `current_user` 只查一次库，不会多打数据库。
    """
    from app.core import quota as quota_mod

    async def _dep(user: User = Depends(current_user)) -> User:
        # 独立 session 查权限：不搭请求内 get_db 的事务，避免端点回滚把
        # 权限查询拖下水（也避免这里的提交影响业务事务）
        async with SessionLocal() as db:
            perms, quotas = await load_permissions(db, user)

        if perm is not None and not has_perm(perms, perm):
            raise HTTPException(403, "没有该功能的使用权限，请联系管理员")
        if quota is not None:
            await quota_mod.consume(user.id, quota, quotas.get(quota))
        return user

    return _dep


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
