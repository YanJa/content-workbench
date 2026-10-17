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

from sqlalchemy import Boolean, JSON, String
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

    # 权限覆盖：{"grant": [...], "deny": [...]}，NULL = 完全继承角色模板。
    # **用增量而不是全量快照**：快照会把用户钉死在「管理员改他那一刻」的角色
    # 状态上，之后角色模板新增功能他永远收不到（冻结陷阱）。
    # ⚠️ 这一列的语义是「非 NULL 即生效」，补列 DDL 绝不能带 DEFAULT。
    perm_override: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # 配额覆盖：{"layout.run": {"period": "day", "limit": 10}}，逐功能覆盖，
    # 没提到的功能继续用角色模板的。NULL = 全部继承。
    quota_override: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # 注册时使用的邀请码（渠道统计锚点）。存码本体（去重后），
    # 渠道备注 / 停用态从 invite_codes 表实时反查 —— 不冗余存备注，
    # 管理员改了备注，用户列表立刻跟着变。
    # 可空：管理员代建、系统预置 admin、旧码迁移前的存量用户都没有。
    invite_code: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None, index=True)

    created_at: Mapped[int] = mapped_column(default=_now)
    last_login_at: Mapped[int | None] = mapped_column(nullable=True)
