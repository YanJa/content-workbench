"""角色权限模板（RBAC 的配置侧）。

**这张表是覆盖层，不是唯一真源。** 权限点的清单和「各角色默认有什么」写在
`core/rbac.py` 的常量里；这张表为空时，代码里的默认值照样生效。

为什么必须这样：如果以表为准，新表建好的那一刻它是空的，存量 member 会
瞬间失去全部功能 —— 那是 fail-closed，一次部署就把全站功能关了。表只在
管理员明确改过某个角色时才写一行。

配额（quotas）也放这里：per-feature 的 `{feature: {period, limit}}`。
`limit` 缺失 / 0 / 负数都表示**不限**，绝不表示「0 次」。
"""
from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class RolePermission(Base):
    """某个角色的权限与配额模板。"""

    __tablename__ = "role_permissions"

    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    # 权限点 key 列表。空列表 = 明确的「什么都没有」（管理员可以这么设）。
    permissions: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    # {"chat": {"period": "day", "limit": 50}, "video": {"period": "month", "limit": 10}}
    quotas: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
