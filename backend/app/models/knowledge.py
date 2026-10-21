"""知识库模型（RAG 素材）：条目 + 片段两层。

为什么是两张表而不是一张：
  - 用户视角以「条」为单位：列表、改名、删除、看状态，都在 item 上；
  - 检索视角以「片」为单位：向量、打分、命中，都在 chunk 上。
  合成一张会让「重跑某条失败的分片」退化成「整条重建」，也会让列表页
  要背着一坨向量数据走。

两层都冗余 owner：检索时按 owner 过滤免 join，也不必担心跨用户读到。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import JSON, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex


class KnowledgeItem(Base):
    """一条素材（用户视角的「条」）。

    status 流转：pending → processing → ready / partial / failed。
    partial = 部分片段向量化失败，仍可检索（命中的是已就绪那部分）。
    """

    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)

    # manual（手动录入）/ content（复用成稿）/ upload（上传文档）/ url（网页抓取）
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    # 来源线索：content 存 content_id，url 存链接，upload 存原始文件名
    origin_ref: Mapped[str] = mapped_column(Text, default="")
    # 抽取出的原文：留着是为了回看，以及将来换分块策略可以重切
    raw_text: Mapped[str] = mapped_column(Text, default="")
    # 仅 upload：原始文件相对 UPLOAD_DIR 的路径
    file_path: Mapped[str | None] = mapped_column(String(300), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)

    __table_args__ = (
        Index("ix_kb_item_owner_time", "owner", "created_at"),
        Index("ix_kb_item_owner_source", "owner", "source"),
    )


class KnowledgeChunk(Base):
    """一个片段（检索视角的最小单位）。

    vector 存 1024 维浮点列表（4 位小数压缩，约 7KB/片）；
    norm 预存模长，检索时只算点积即可得余弦，省掉每次开方。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)

    content: Mapped[str] = mapped_column(Text, default="")
    char_len: Mapped[int] = mapped_column(Integer, default=0)

    vector: Mapped[list | None] = mapped_column(JSON, nullable=True)
    norm: Mapped[float] = mapped_column(Float, default=0.0)
    # 记下用的是哪个模型：将来换 embedding 模型，能分辨哪些片需要重算
    embed_model: Mapped[str] = mapped_column(String(64), default="")

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[int] = mapped_column(default=_now)

    __table_args__ = (
        Index("ix_kb_chunk_owner_item", "owner", "item_id"),
        Index("ix_kb_chunk_owner_status", "owner", "status"),
    )
