"""功能用量计数（配额的执行侧）。

为什么单独一张表，而不是复用 LlmUsage / RenderJob：
  配额要统计的是**业务动作次数**（发了几次对话、提了几次渲染），而 LlmUsage
  统计的是**模型调用次数** —— 一轮提问可能触发 N 次调用（工具来回、历史压缩），
  拿它当「次数」会莫名其妙地翻倍。RenderJob 倒是 1:1，但它只覆盖视频。

计数口径
--------
**提交时预扣，不是成功时记账。** 五个受控功能全是异步/流式：渲染进后台任务、
排版进内存任务表、复刻是 create_task、对话是 SSE。等「成功」才知道要不要记，
等于允许反复提交失败任务零成本刷爆 GPU 和 token —— 那正是配额要防的事。

代价是「任务失败也扣了」。补偿办法只有一处：推送草稿是同步可判成败的，
明确 `ok=False` 时调 `refund()` 还回去。其余一律不退（资源已经真实消耗）。
真要人工纠正，管理员在用户管理页点「重置用量」。

行键设计
--------
`(owner, feature, period, period_key)` 唯一。周期是 day 还是 month 由配额
配置决定，两种周期各存一行、互不干扰 —— 改周期时旧计数自然失效，不用清理。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uid() -> str:
    return uuid.uuid4().hex


class QuotaUsage(Base):
    """某用户在某个周期内，某个功能用了多少次。"""

    __tablename__ = "quota_usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    feature: Mapped[str] = mapped_column(String(32))
    # 'day' | 'month'
    period: Mapped[str] = mapped_column(String(8))
    # '2026-10-17' | '2026-10'（CST，见 core/quota.py 的 period_key）
    period_key: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))

    __table_args__ = (
        UniqueConstraint("owner", "feature", "period", "period_key", name="uq_quota_period"),
        Index("ix_quota_owner_feature", "owner", "feature"),
    )
