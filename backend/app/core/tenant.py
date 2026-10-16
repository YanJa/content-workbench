"""租户（数据归属）解析 —— 用户模块的唯一身份接缝。

背景
----
所有业务表早有 `owner` 列。用户模块上线前系统是「单租户」：owner 由
客户端通过 `X-Owner-Id` 头 / `?owner=` 查询参数**自称**，零验证
（git 历史里的旧版注释自述「这是隔离接缝，不是安全边界」）。现在替换为
服务端解析身份：

1. 普通请求：`Authorization: Bearer <JWT>` → 解析出 User → `owner = user.id`。
2. 浏览器直链（`<img>` / `<audio>` / `<video>` 的 src）没法带请求头：
   URL 带 `?o=<uid>&sig=<hmac>`，HMAC 校验通过即放行（签名生成见
   `core/security.py` 的 `link_url`）；也允许带 Bearer 头访问
   （同源前端 fetch 场景）。
3. 都没有 → 普通端点 401；直链端点 404（不泄露资源是否存在）。

约定
----
- `current_user` 返回 **User 对象**而不是 id：个别端点需要 username 等
  字段时不用再查库。
- `link_auth` 返回 owner 字符串：直链端点只关心归属，路径匹配靠签名
  本身完成（签名绑 `request.url.path`，见 `link_ok`）。
- `normalize_owner` 保留：owner 会进文件路径（音色目录按 owner 隔离、
  slide folder 等），字符白名单校验仍需要；用户模块后 owner 一律来自
  服务端，这个函数主要给路径段校验用。

停用即时生效
------------
`role` 和 `is_active` 都**不进 JWT**（详见 core/rbac.py 的说明）：令牌
有效期 7 天，签进去就得等 7 天才生效。所以每次请求都查一次库，顺手校验
`is_active` —— 被停用的人手里的旧 token 当场失效，不用等它自然过期。
"""
from __future__ import annotations

import re

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, link_ok
from app.db.session import get_db
from app.models.user import User

DEFAULT_OWNER = "default"

# owner 会用于拼文件路径，只放行安全字符
_OWNER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def normalize_owner(raw: str | None) -> str:
    """把外部传入的 owner 规范成合法值；不合法直接抛 ValueError。"""
    v = (raw or "").strip()
    if not v:
        return DEFAULT_OWNER
    if not _OWNER_RE.fullmatch(v):
        raise ValueError(f"非法的 owner 标识（仅限 1~64 位字母、数字、._-）：{raw!r}")
    return v


def _bearer_user_id(authorization: str | None) -> str | None:
    """从 Authorization 头里解出 user_id；形似 Bearer 才尝试，否则 None。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return decode_token(authorization[7:].strip())


async def current_user(
    authorization: str | None = Header(default=None, description="Bearer <JWT>，登录后由前端自动携带"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI 依赖：从登录态解析当前用户。

    用户模块落地后这是唯一身份接缝：所有 `Depends(current_user)` 的端点
    拿到的 owner 都是服务端解析出的 user.id，不再信任客户端传入值。
    Token 缺失 / 无效 / 过期 / 用户被删 → 401。
    """
    user_id = _bearer_user_id(authorization)
    if not user_id:
        raise HTTPException(401, "未登录或登录已过期，请重新登录")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "账号不存在或已被删除")
    if not user.is_active:
        raise HTTPException(401, "账号已被停用，请联系管理员")
    return user


async def current_owner_id(user: User = Depends(current_user)) -> str:
    """FastAPI 依赖：当前用户的数据归属 id。

    `current_user` 返回 User 对象（个别端点要 username 等字段）；这个薄封装
    返回 `user.id`，让用户模块上线前写成 `owner: str = Depends(current_owner)`
    的端点签名一行不用动 —— 函数体里 owner 照旧当字符串用。
    """
    return user.id


async def link_auth(
    request: Request,
    o: str | None = Query(default=None, description="直链归属（用户 id）"),
    sig: str | None = Query(default=None, description="直链 HMAC 签名"),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> str:
    """FastAPI 依赖：浏览器直链（img/audio/video 的 src）鉴权。

    优先级：HMAC 签名 > Bearer 头。签名绑 `request.url.path`（不含
    query），所以每个 URL 只能访问它签名时对应的那个路径。两者都没有
    → 404：对匿名访问者而言「资源不存在」，不泄露存在性。

    停用的用户只影响 Bearer 那一档：**HMAC 分支刻意不动**。历史图片的
    URL 已经写进 `contents.body_html` / `contents.images`，签名绑的是
    `o=<uid>`；停用就封会让存量文章的配图集体 404，那是牵连无辜读者，
    不是惩罚被停用的人。
    """
    if o and sig:
        if link_ok(o, request.url.path, sig):
            return o
        raise HTTPException(404, "资源不存在")
    user_id = _bearer_user_id(authorization)
    if user_id:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is not None and user.is_active:
            return user.id
    raise HTTPException(404, "资源不存在")
