"""用户模型。

用户模块的落地形态：一张极简的表 —— 用户名 + bcrypt 密码哈希 + 角色。

用户 id 用 `uuid4().hex`（32 字符），直接当数据归属（owner）用：
所有业务表早就有 `owner String(64)` 列，用户体系上线后
「owner = 当前登录用户的 id」，隔离口径从「客户端自称」变成
「服务端解析」（见 app/core/tenant.py）。

为什么只加 role / is_active 两个字段而不是一整套（邮箱 / 手机 / 昵称 /
头像 / 部门）：这个工作台是单人自用 + 邀请制小团队的工具，不是 To C 产品。
字段按需再加，表越简单迁移越省事 —— 角色是因为要做 RBAC 才加的，
停用是为了「保留数据但收回门禁」而加的，其余仍然没有。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class User(Base):
    """一个登录用户。username 唯一，冲突时注册接口返回 409。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # bcrypt 哈希固定 60 字符（$2b$ + 成本因子 + 22 位 salt + 31 位摘要）
    password_hash: Mapped[str] = mapped_column(String(60))

    # 角色：只有 'admin' / 'member' 两级，常量见 app/core/rbac.py。
    # 这里 default 写字面量而不 import rbac —— rbac 依赖 models，
    # models 再 import rbac 就是循环导入了。两边值必须一致。
    role: Mapped[str] = mapped_column(String(16), default="member")
    # 停用：保留这个人的一切数据，只收回门禁（登录 + 所有 Bearer 请求）。
    # 刻意不删行 —— 存量直链签名绑 `o=<uid>`，删了用户的配图会集体 404。
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[int] = mapped_column(default=_now)
    last_login_at: Mapped[int | None] = mapped_column(nullable=True)
