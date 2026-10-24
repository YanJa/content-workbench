"""内容与发布队列模型（沿用 demo 期的数据结构，迁移到服务端）。"""
from __future__ import annotations

import time

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class Content(Base):
    """一条内容成品（文案 / 视频元数据）。

    字段分两层：
      - 纯内容层：body（结构化 Markdown）—— 对话创作的产出；
      - 排版层：body_html / theme / layouts / images —— 发布队列里「加工」的产出。

    分离的原因（2026-10-16 重构）：对话创作只负责写内容，排版加工收到队列页做。
    这样「生成」和「美化成可发布形态」是两步，用户满意了内容再花时间排版，不浪费。
    """

    __tablename__ = "contents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)  # gzh/xiaohongshu/douyin/zhihu
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text / video
    # 平台内的子形态。目前只有知乎用：answer（回答）/ article（文章专栏）/ idea（想法）。
    # 其余平台恒为空串 —— 一个知乎在统计上是一条平台，形态差异不该炸成三个平台。
    sub_type: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    # 纯内容正文：结构化 Markdown（# 标题 / > 导语 / ## 小标题 / 正文）
    body: Mapped[str] = mapped_column(Text, default="")
    # 导语/摘要（列表页与微信 digest 用）
    summary: Mapped[str] = mapped_column(Text, default="")
    # 生成元信息：模型、耗时、主题等
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 来源会话（可从对话一键转成品）
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ---------- 排版产物（队列加工后写入） ----------
    # 排版后的内联样式 HTML（公众号兼容的 <section>…</section> 片段）
    body_html: Mapped[str] = mapped_column(Text, default="")
    # 所用主题标识（moyu-green / red-white / ...）
    theme: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 多平台排版快照：{"gzh": {...}, "xiaohongshu": {...}}，一份内容可同时有两种成稿形态
    layouts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 配图列表：[{id,path,url,alt,source,prompt,position}]
    images: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # none（未排版）/ ready（已排版，可发布）
    layout_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    # 加工中标记：""（空闲）/ "layout"（排版中）/ "image"（生图中）
    # 与 layout_status 分开：一个表示「正在做什么」（瞬时），一个表示「做完没」（产物态）。
    # 队列页据此显示「排版中/生图中」并禁用相关操作，避免重复触发。
    processing: Mapped[str] = mapped_column(String(16), default="", index=True)

    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)


class QueueItem(Base):
    """发布队列项。"""

    __tablename__ = "queue_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    content_id: Mapped[str] = mapped_column(String(36), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    # 子形态：决定这条发出去是「回答」还是「文章」，队列页据此给发布深链。
    # 与 Content.sub_type 同源，入队时复制一份（队列页不该为了显示去 join 内容表）。
    sub_type: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    # draft(待加工) / ready(待发布) / published(已发布) / archived(已归档)
    # 新建即 draft —— 内容先进队列，用户加工排版后才「待发布」（2026-10-16 起）
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # 各平台发布前检查清单的完成情况
    checklist: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 推送草稿箱后的微信 media_id
    wechat_media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    created_at: Mapped[int] = mapped_column(default=_now)
    published_at: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_queue_owner_status", "owner", "status"),)
