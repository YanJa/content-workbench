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

2026-10-19 又补了 nickname / email：用户中心（顶栏下拉 → 个人中心）要
改自己的资料。两个都可空 —— 存量用户没填过，用户名仍是唯一登录凭据；
email **不用于登录、不做唯一索引**（NULL 在 SQLite/Postgres 里本就允许多条，
强制唯一得建部分索引，应用层查重就够：个人中心保存时查一次重名）。
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

    # 昵称（展示用）：顶栏、个人中心显示它而不是 username。可空 = 未设置，
    # 前端一律 `nickname || username` 兜底。**不唯一** —— 重名无所谓，
    # 数据隔离靠 id 不靠名字。
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # 头像（展示用）：存**相对 UPLOAD_DIR 的路径** `avatars/<uid>.jpg`，
    # 可空 = 未设置（前端回落成「用户名首字母」圆徽）。上传时由后端统一
    # 居中裁剪成 256×256 JPEG（见 routers/auth.py 的 /auth/avatar），
    # 所以路径后缀恒定 .jpg —— 不保留用户上传的原格式/原尺寸。
    avatar: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    # 邮箱（联系方式）：不用于登录、不建唯一索引（NULL 可重复），保存时
    # 应用层查重（见 routers/auth.py 的 PUT /auth/profile）。
    email: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)

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
