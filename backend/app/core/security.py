"""认证与直链签名的安全原语。

三块内容都集中在这里，避免散落各处风格漂移：

1. **密码**：bcrypt 哈希 / 校验。直接用 bcrypt 库，不引 passlib ——
   passlib 1.7.4 与 bcrypt 5.x 有已知兼容问题（读不到 bcrypt 版本信息），
   没必要为一个哈希函数背这个风险。
2. **Token**：JWT(HS256) 签发 / 校验。secret 优先级：
   `settings.JWT_SECRET`（.env 显式配置）> `data/secret.key`（首次启动
   自动生成并持久化）。自动生成而不是拍默认常量：默认常量会进 git
   历史，等于公开签名密钥；每次随机又会让重启后所有 token 失效。
3. **直链签名**：`<img>` / `<audio>` 的 src 没法带 Authorization 头，
   用 HMAC-SHA256(secret, owner + "\\n" + path) 的截断 hex 当能力凭证。
   不过期（否则存量 URL 会集体失效）；泄露代价 = 该用户该路径可读，
   比 JWT-in-URL（全账户凭证进 Referer / 历史记录）小一个量级。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

import bcrypt
import jwt

from app.core.config import settings
from app.core.logging import logger

_ALGORITHM = "HS256"
# 与 DATABASE_URL（sqlite:///./data/...）同源：uvicorn 从 backend/ 启动，
# 这里就落在 backend/data/secret.key，已被 .gitignore 拦住
_SECRET_FILE = Path("./data/secret.key")


def _load_or_create_secret() -> str:
    """显式配置优先；否则读持久化文件；没有再自动生成（0600）。"""
    if settings.JWT_SECRET:
        return settings.JWT_SECRET
    try:
        if _SECRET_FILE.is_file():
            saved = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except OSError:
        pass  # 读失败就走到生成分支
    secret = secrets.token_hex(32)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secret, encoding="utf-8")
        _SECRET_FILE.chmod(0o600)
        logger.info("已生成 JWT/HMAC 密钥并持久化到 %s（重启不失效；配置 JWT_SECRET 可覆盖）", _SECRET_FILE)
    except OSError:
        # 写不进文件系统（只读挂载等）：退回进程内随机，本次运行内仍一致
        logger.warning("无法持久化密钥到 %s，使用进程内随机密钥（重启后全部 token 失效）", _SECRET_FILE)
    return secret


_SECRET = _load_or_create_secret()


# ---------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # 哈希格式损坏（库被手动改过等）：按校验失败处理，不抛 500
        return False


# ---------------------------------------------------------------- Token
def create_token(user_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + settings.JWT_TTL_HOURS * 3600,
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> str | None:
    """返回 user_id；无效 / 过期返回 None（由调用方决定 401）。"""
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


# ---------------------------------------------------------------- 直链签名
def _sig(owner: str, path: str) -> str:
    """owner 与路径绑在一起签名：拿到一个 URL 只能读这一个路径。"""
    key = _SECRET.encode("utf-8") if isinstance(_SECRET, str) else _SECRET
    return hmac.new(key, f"{owner}\n{path}".encode(), hashlib.sha256).hexdigest()[:32]


def link_url(path: str, owner: str) -> str:
    """给需要浏览器直链的路径签名。path 不带 query，如 /api/layout/images/xxx.png。"""
    return f"{path}?o={owner}&sig={_sig(owner, path)}"


def link_ok(owner: str, path: str, sig: str | None) -> bool:
    """常量时间比较，防时序侧信道。"""
    if not owner or not sig:
        return False
    return hmac.compare_digest(_sig(owner, path), sig)
