"""素材库模型。

三条设计决定，改之前先想清楚：

1. **只存 storage_key，不存完整 URL**。现有 `Content.images[].url` 是「算出来再存」的，
   那是历史包袱；素材库不沿用。原因：签名 URL 有有效期（过期就是死链）、换存储后端
   （本地↔云）就得改一遍数据。只存 `assets/<owner>/<yyyymm>/<id>.jpg` 这样的相对 key，
   读取时按当前后端临时签发。

2. **hash 唯一（按 owner）**。同一张图从 Pixabay 和 Openverse 各导入一次、或用户重复上传
   同一张，都只留一份。用 `(owner, hash_sha256)` 唯一约束而不是查一遍再插 —— 并发导入时
   「先查后插」会漏，唯一约束才是可靠的。

3. **original_key 永不覆盖**。去水印、裁切这类加工产物另存新 key，原图始终可回溯。
   合规上这是硬要求（见 docs/素材库模块调研与实现方案.md 第 5 节），不是可选项。
"""
from __future__ import annotations

import time

from sqlalchemy import JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class Asset(Base):
    """一条素材（图片 / 视频）。

    字段分四组：来源与授权、内容属性、存储位置、使用统计。
    分组不是形式主义 —— 前两组决定「能不能用」（合规），后两组决定「怎么取」（工程）。
    """

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)

    kind: Mapped[str] = mapped_column(String(16), default="image", index=True)  # image / video

    # ---------- 来源与授权（合规依据，别删） ----------
    # upload（用户上传）/ search（在线图源导入）/ ai（生图）/ cover（封面产物）
    source: Mapped[str] = mapped_column(String(16), default="upload", index=True)
    # pixabay / openverse / coverr / unsplash / ""（自有素材）
    source_name: Mapped[str] = mapped_column(String(24), default="")
    # 源站素材 ID：同一素材重复搜索时用于去重（比 hash 早一步，不用先下载）
    source_id: Mapped[str] = mapped_column(String(128), default="")
    # 原页面地址：署名回链、侵权下架时追溯用
    source_url: Mapped[str] = mapped_column(Text, default="")
    # pixabay-content / cc0 / cc-by / cc-by-sa / own / unknown
    license: Mapped[str] = mapped_column(String(32), default="unknown")
    license_url: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(128), default="")
    author_url: Mapped[str] = mapped_column(Text, default="")
    # 现成的署名行（如 "Image by Hans on Pixabay"），发布时可原样附上
    attribution_text: Mapped[str] = mapped_column(Text, default="")

    # ---------- 内容属性 ----------
    title: Mapped[str] = mapped_column(String(300), default="")
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    width: Mapped[int] = mapped_column(default=0)
    height: Mapped[int] = mapped_column(default=0)
    duration: Mapped[int] = mapped_column(default=0)  # 视频秒数，图片为 0
    size_bytes: Mapped[int] = mapped_column(default=0)
    mime: Mapped[str] = mapped_column(String(64), default="")
    # 内容指纹：跨源去重的最终依据（同一张图不同 URL 也能识别）
    hash_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)

    # ---------- 存储 ----------
    storage: Mapped[str] = mapped_column(String(16), default="local")  # local / s3
    storage_key: Mapped[str] = mapped_column(String(255), default="")
    thumb_key: Mapped[str] = mapped_column(String(255), default="")
    # 加工前原图（去水印/裁切后仍保留）。空串 = 未经加工
    original_key: Mapped[str] = mapped_column(String(255), default="")
    # none（无）/ removed（已去水印）。P2 起使用
    watermark_state: Mapped[str] = mapped_column(String(16), default="none")

    # ---------- 使用统计 ----------
    used_count: Mapped[int] = mapped_column(default=0)
    last_used_at: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)

    __table_args__ = (
        # 同一用户下同内容只存一份。唯一约束而非「先查后插」：并发导入会漏。
        UniqueConstraint("owner", "hash_sha256", name="uq_asset_owner_hash"),
        Index("ix_asset_owner_kind", "owner", "kind"),
        Index("ix_asset_owner_created", "owner", "created_at"),
    )
