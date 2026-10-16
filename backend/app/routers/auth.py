"""认证路由：注册 / 登录 / 当前用户。

注册策略（settings.AUTH_REGISTER_MODE）
------------------------------------
- **第一个用户（users 表为空）永远放行**，并在同一事务里「认领」存量数据
  （owner='default' → 该用户 id）—— 这是单租户 Demo 期平滑过渡的关键一步，
  否则老数据会挂在已废弃的 default 租户下，新用户什么都看不到。
- 之后按模式：`invite` = 校验 INVITE_CODE（未配置邀请码时禁止注册）；
  `open` = 开放；`closed` = 禁止。

登录限流：按用户名计数，5 次 / 15 分钟。内存实现、重启清零 —— 对这个
量级的工作台够用；真被打爆了再挂 Redis（项目已有 cache 层）。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.core.security import create_token, hash_password, link_url, verify_password
from app.core.tenant import current_user
from app.db.session import get_db
from app.models.content import Content, QueueItem
from app.models.conversation import Conversation
from app.models.renderjob import RenderJob
from app.models.settings import AppSetting, WeChatAccount
from app.models.usage import LlmUsage
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["认证"])


# ---------------------------------------------------------------- 模型
class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    invite_code: str = ""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    created_at: int


class TokenOut(BaseModel):
    token: str
    user: UserOut
    # 仅首用户注册时非空：告知用户本次接管了多少存量数据
    claimed: dict | None = None


# ---------------------------------------------------------------- 校验与限流
_USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")

_LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_WINDOW_SEC = 15 * 60


def _validate_username(raw: str) -> str:
    v = (raw or "").strip()
    if not _USERNAME_RE.fullmatch(v):
        raise HTTPException(422, "用户名只能包含字母、数字、_ 或 -，长度 3~32 位")
    return v


def _check_rate(username: str) -> None:
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS[username] if now - t < _WINDOW_SEC]
    _LOGIN_ATTEMPTS[username] = attempts
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(429, "登录失败次数过多，请 15 分钟后再试")


def _record_fail(username: str) -> None:
    _LOGIN_ATTEMPTS[username].append(time.time())


# ---------------------------------------------------------------- 存量认领
# 所有带 owner 列的表（messages 无 owner，随 conversation 一起归属）
_OWNER_MODELS = [Content, QueueItem, Conversation, RenderJob, AppSetting, WeChatAccount, LlmUsage]

# 存量直链形态：<path>?owner=default（owner 是唯一 query 参数）。
# 捕获组只吃 URL 本体，不碰引号 / 尖括号 / 空白，JSON 与 HTML 里都安全。
_LEGACY_URL_RE = re.compile(r"([^\s\"'<>]+?)\?owner=default\b")


def _rewrite_urls_in_text(text: str, uid: str) -> tuple[str, int]:
    """把文本里所有 `?owner=default` 直链重签成 `?o=<uid>&sig=...`。"""

    def _repl(m: re.Match) -> str:
        return link_url(m.group(1), uid)

    new_text, n = _LEGACY_URL_RE.subn(_repl, text)
    return new_text, n


async def _claim_legacy_data(db: AsyncSession, uid: str) -> dict:
    """首用户注册时，把单租户期的存量数据判给该用户（同事务）。

    三件事：
    1. 所有 `owner='default'`（含 NULL）的行改判为 uid；
    2. 持久化 URL（contents.images / layouts JSON、body_html）重签 ——
       不重签的话存量直链会在认领后全部失效；
    3. 音色目录 `data/voices/profiles` → `data/voices/owners/<uid>/profiles`
       （services/voices.py 的 profiles_dir 早有 owners/<owner> 分支，
       所以这里是纯数据搬迁，代码零改）。
    """
    stats = {"rows": 0, "urls": 0, "voice_dir_moved": False}

    for model in _OWNER_MODELS:
        result = await db.execute(
            update(model)
            .where((model.owner == "default") | (model.owner.is_(None)))
            .values(owner=uid)
        )
        stats["rows"] += result.rowcount or 0

    # URL 重写：JSON 列 dumps 成字符串统一做正则，解析失败就原样跳过
    contents = (await db.execute(select(Content).where(Content.owner == uid))).scalars().all()
    for c in contents:
        changed = False
        for col in ("images", "layouts"):
            raw = getattr(c, col, None)
            if not raw:
                continue
            text = json.dumps(raw, ensure_ascii=False)
            new_text, n = _rewrite_urls_in_text(text, uid)
            if not n:
                continue
            try:
                setattr(c, col, json.loads(new_text))
            except (ValueError, TypeError):
                logger.warning("URL 重写后 JSON 解析失败，跳过该列 content=%s col=%s", c.id, col)
                continue
            stats["urls"] += n
            changed = True
        if c.body_html and "?owner=default" in c.body_html:
            new_html, n = _rewrite_urls_in_text(c.body_html, uid)
            if n:
                c.body_html = new_html
                stats["urls"] += n
                changed = True
        if changed:
            c.updated_at = int(time.time())

    base = Path(settings.VOICE_DIR)
    legacy = base / "profiles"
    target = base / "owners" / uid / "profiles"
    if legacy.is_dir() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        stats["voice_dir_moved"] = True

    return stats


# ---------------------------------------------------------------- 端点
@router.post("/register", response_model=TokenOut)
async def register(req: RegisterIn, db: AsyncSession = Depends(get_db)):
    """注册。

    - 首用户永远放行（并触发存量认领）；
    - 之后按 AUTH_REGISTER_MODE：invite 校验邀请码 / open 放行 / closed 拒绝。
    """
    username = _validate_username(req.username)
    if len(req.password) < 8:
        raise HTTPException(422, "密码至少 8 位")

    first_user = (await db.execute(select(User.id).limit(1))).first() is None

    taken = (await db.execute(select(User.id).where(User.username == username))).first()
    if taken:
        raise HTTPException(409, "用户名已被占用")

    if not first_user:
        mode = settings.AUTH_REGISTER_MODE
        if mode == "closed":
            raise HTTPException(403, "注册通道已关闭")
        if mode == "invite":
            expected = settings.INVITE_CODE
            if not expected or req.invite_code.strip() != expected:
                raise HTTPException(403, "邀请码不正确")

    user = User(username=username, password_hash=hash_password(req.password))
    db.add(user)
    await db.flush()  # 拿到 user.id，先不提交（认领要和注册同事务）

    claimed: dict | None = None
    if first_user:
        claimed = await _claim_legacy_data(db, user.id)

    await db.commit()
    logger.info("新用户注册: %s (id=%s, 首用户=%s)", username, user.id, first_user)
    if claimed and (claimed["rows"] or claimed["urls"] or claimed["voice_dir_moved"]):
        logger.info("首用户认领存量数据: %s", claimed)
    return TokenOut(
        token=create_token(user.id),
        user=UserOut(id=user.id, username=user.username, created_at=user.created_at),
        claimed=claimed,
    )


@router.post("/login", response_model=TokenOut)
async def login(req: LoginIn, db: AsyncSession = Depends(get_db)):
    """登录。按用户名限流：5 次 / 15 分钟。"""
    username = (req.username or "").strip()
    _check_rate(username)

    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        _record_fail(username)
        raise HTTPException(401, "用户名或密码错误")

    _LOGIN_ATTEMPTS.pop(username, None)
    user.last_login_at = int(time.time())
    await db.commit()
    return TokenOut(
        token=create_token(user.id),
        user=UserOut(id=user.id, username=user.username, created_at=user.created_at),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    """当前登录用户。前端启动时用它验证本地 token 是否还有效。"""
    return UserOut(id=user.id, username=user.username, created_at=user.created_at)
