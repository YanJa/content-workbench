"""邀请码模型。

邀请码是自助注册的门槛 + 渠道统计的锚点（2026-10 上线）。

一张表装两件事：
- **注册门槛**：`max_uses` 限制一个码能注册多少账号（3 个、10 个……），
  `null` = 不限次。`used` 是已用计数，扣减走「条件 UPDATE」保证并发下不超发。
- **渠道统计**：`note` 是管理员起的备注（如「小红书投放」「朋友圈」），
  注册时把码写进 `users.invite_code`，用户列表据此反查「这个人来自哪个渠道」。

字段语义的几条关键决定（别改，改了会破）：
- `max_uses = null` = 不限次。**绝不能默认 1 或 0** —— fail-closed 会把
  「没填次数」变成「只能注册 0 次」，新码一生成就是废码。
- 用尽 = 停用保留记录，不删行：渠道统计和历史要能查。`disabled` 手动停用
  和「用尽自动停用」共用一个状态位，`used >= max_uses` 时校验层直接拒绝，
  不必真去改这一列（省一次写，读时也少一次歧义）。
- `code` 唯一。生成时撞了唯一约束就重试（见 core/invite.py 的 gen）。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class InviteCode(Base):
    """一条可复用的邀请码。"""

    __tablename__ = "invite_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    # 邀请码本体。管理员生成时自定义，或留空由后端随机（见 core/invite.py）。
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 渠道备注，如「小红书投放」「知乎引流」。可空。
    note: Mapped[str] = mapped_column(String(200), default="")
    # 最大可用次数；null = 不限。
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # 已用次数。扣减在 core/invite.py 里走原子条件 UPDATE，这里只是落库口径。
    used: Mapped[int] = mapped_column(Integer, default=0)
    # 手动停用（渠道取消合作 / 作废）。用尽不算停用，是校验层即时拒绝。
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 谁生成的（管理员 id），用于审计。可空（自动导入的旧码没有创建人）。
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True, default=None)
    created_at: Mapped[int] = mapped_column(default=_now)
