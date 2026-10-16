"""渲染任务。

为什么落库而不是放缓存：
  渲染是几十秒到几分钟的重活，而进程会因为 supervisord 重启、代码更新、
  崩溃自愈等原因消失。之前放 Redis/内存缓存，一重启任务就凭空蒸发，
  前端还在轮询却永远等不到结果 —— 用户看到的是"卡在 65% 不动"。

落库后即使服务重启，任务列表和成片路径都还在，最多把中断的任务标成失败，
而不是让它悬念般地挂着。任务量很小（日均几十条），SQLite 完全够。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex


class RenderJob(Base):
    """一次视频渲染任务。

    params 存 JSON（script / ratio / voice / subtitle / source…），
    用于「重新渲染」时还原参数，不用让用户再填一遍。
    """

    __tablename__ = "render_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="default")

    # queued / running / done / failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(64), default="排队中")
    error: Mapped[str] = mapped_column(Text, default="")

    # 入参与产物
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    output_path: Mapped[str] = mapped_column(String(512), default="")

    # 成片观测值，回显给用户（也是排查素材是否正常的依据）
    duration_sec: Mapped[float] = mapped_column(default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    title: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(16), default="text")  # text | ppt

    created_at: Mapped[int] = mapped_column(Integer, default=_now)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_job_owner_created", "owner", "created_at"),)
