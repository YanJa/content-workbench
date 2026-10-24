"""LLM 用量记录。

为什么要独立一张表，而不是往 messages 上加字段：
  一次用户提问可能触发 **N 次** 模型调用 —— 工具来回、历史压缩、不同任务路由到
  不同模型。messages 一行存不下这种一对多的关系，而按时间聚合又是用量模块的主查询。

每次调用一行，落库后只读不改（成本事实，不回溯修改）。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex


class LlmUsage(Base):
    """一次 LLM 调用的用量与成本事实。"""

    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)

    # 归属（都可为 NULL：历史压缩调用没有 message，匿名调用没有会话）
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    owner: Mapped[str] = mapped_column(String(64), default="default")
    scene: Mapped[str] = mapped_column(String(32), default="chat")

    # 调用特征
    model: Mapped[str] = mapped_column(String(64), default="")
    task_type: Mapped[str] = mapped_column(String(32), default="")  # copywriting/reasoning/...
    # 这次调用烧的是谁的 Key："system" = 平台全局凭证，否则是用户 id。
    # 用于事后归因（这个月平台 Key 花了多少 / 谁用自己的 Key 花了多少）。
    # ⚠️ 不用于实时额度判定 —— 额度走 model_quota_usage 计数器，
    #    因为 CF 等免费通道流式拿不到 usage，token 统计不到（见 models/model_pool.py）。
    cred_owner: Mapped[str] = mapped_column(String(64), default="system")

    # 这次调用算哪种账："free" 免费模型（0 元）/ "paid" 有单价 / "unknown" 没登记、按兜底价估。
    # 与 is_peak 一样是**调用当时定格**的：事后把模型改成付费，历史账单不该跟着变。
    # 空串 = 老数据（加列之前的行），读的时候按 unknown 处理。
    billing: Mapped[str] = mapped_column(String(8), default="")

    # token 明细
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 含在 completion 内
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 命中缓存的部分
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)

    # 其他观测
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    is_peak: Mapped[bool] = mapped_column(Boolean, default=False)
    is_summary: Mapped[bool] = mapped_column(Boolean, default=False)  # 历史压缩开销
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)  # 回填/缺失标记

    created_at: Mapped[int] = mapped_column(Integer, default=_now)

    __table_args__ = (
        Index("ix_usage_created", "created_at"),
        Index("ix_usage_model_created", "model", "created_at"),
        Index("ix_usage_conv", "conversation_id"),
    )
