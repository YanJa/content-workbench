"""会话与消息模型。

多轮对话的核心：会话含多条消息，消息带角色与工具调用信息。
"""
from __future__ import annotations

import time

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class Conversation(Base):
    """一个对话会话。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    # 会话归属（一期的简单用户标识，二期可换成真实账号）
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    # 业务场景：chat / gzh / xiaohongshu / douyin
    scene: Mapped[str] = mapped_column(String(32), default="chat", index=True)
    # 更早消息的摘要（用于长会话上下文压缩）
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now, onupdate=_now)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    __table_args__ = (Index("ix_conv_owner_updated", "owner", "updated_at"),)


class Message(Base):
    """一条消息。

    role: system / user / assistant / tool
    工具调用相关字段用于回放和调试 Function Calling 链路。
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    # 推理模型的思维链（逐 delta 累积的完整推理过程）。
    # 只给人看：build_context 不回灌模型（省 token，且避免 provider 兼容问题）。
    reasoning: Mapped[str] = mapped_column(Text, default="")

    # 模型调用失败的错误信息（随这条 assistant 回复持久化）。
    # SSE 一旦 200 就改不了状态码，错误只能以事件形式推给前端 —— 不落库的话，
    # 刷新/重进会话这条错误就没了，出问题无法回溯（2026-10-23 用户要求记录）。
    # 回灌上下文时跳过（见 services/conversation.build_context）：错误占位行
    # 往往没有正文，塞给模型只会干扰它。
    error: Mapped[str] = mapped_column(Text, default="")

    # Function Calling 相关
    tool_calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 计费用：记录 token 消耗
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[int] = mapped_column(default=_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
