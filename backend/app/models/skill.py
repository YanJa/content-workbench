"""Skill 模型：用户上传的一套「技能包」。

为什么是一张表存一个 skill 而不是拆成 skill + skill_file：
  - 99% 的自制 skill 就是一个 SKILL.md，文件树只是锦上添花；
  - 技能包整体替换（重新上传一次）是主要更新方式，拆表反而要处理一堆脏文件；
  - 完整正文体量很小（官方建议 description ≤1024 字符，正文几千字量级），
    直接存 Text 比落盘再回读简单得多，也免了路径穿越的心智负担。

`name` 唯一索引按 (owner, name)：不同用户可以有同名 skill，互不干扰。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex


class Skill(Base):
    """一套技能包。

    status 流转：ready / invalid。
      - ready：frontmatter 合法，可被模型激活；
      - invalid：解析失败（name 不合规、description 缺失等），error 里写明原因。
        留行而不删，是因为用户上传后需要一个地方看到「为什么没生效」。
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)

    # catalog 里暴露给模型的标识。规则见 services/skill.py: 1-64 字符、仅 a-z0-9-
    name: Mapped[str] = mapped_column(String(64), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # SKILL.md 的正文（去掉 frontmatter）—— 激活时原文喂给模型
    body: Mapped[str] = mapped_column(Text, default="")

    # 打包进来的附属文件：[{"path": "references/x.md", "size": 123}]。
    # 只记清单不存内容：Skill 阶段一只做「正文注入」，附属文件按需再扩展。
    files: Mapped[list | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="ready", index=True)
    error: Mapped[str] = mapped_column(Text, default="")

    # 用了多少次（activate_skill 命中就 +1），用来判断哪些 skill 是死代码
    use_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)

    __table_args__ = (
        UniqueConstraint("owner", "name", name="uq_skill_owner_name"),
        Index("ix_skill_owner_enabled", "owner", "enabled"),
    )
