"""应用级设置模型（公众号账号 + 通用键值设置）。

为什么公众号账号要落库而不是放 .env：
  1. 一个号要记 4 个字段（appid/secret/名称/主页），.env 表达力差、改一次要重启；
  2. 用户可能同时运营多个号（主号 + 矩阵），.env 只能配一个；
  3. 系统状态页要能直接增删改 + 测试连通性，落库才好做。

安全：secret 落本机 SQLite（非公网库），接口响应一律脱敏，不回传明文。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class WeChatAccount(Base):
    """一个微信公众号。

    多号场景：`is_default=True` 的那条是默认推送号；推送时若不指定 account_id，
    就用默认号；一个号都没有时回退到 .env 的 WECHAT_APPID / WECHAT_SECRET。
    """

    __tablename__ = "wechat_accounts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uid)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)

    name: Mapped[str] = mapped_column(String(120), default="")  # 展示名，如「主号·内容运营」
    appid: Mapped[str] = mapped_column(String(64), default="")
    secret: Mapped[str] = mapped_column(Text, default="")  # 不脱敏存库，响应里再脱敏
    homepage: Mapped[str] = mapped_column(String(300), default="")  # 主页 / 简介，可选

    # ---- 关注引导（正文文末关注卡用）----
    # 对外公众号名：替换组件库的 {{号名}}。与 name（运营内部展示名）语义不同，
    # 因此单开一列，不复用 name。
    gzh_name: Mapped[str] = mapped_column(String(120), default="")
    # 关注卡副标：替换组件库的 {{slogan}}。
    slogan: Mapped[str] = mapped_column(String(200), default="")
    # 公众号二维码图，存相对 UPLOAD_DIR 的路径（如 qrcodes/{id}/xxx.png）。
    # 存路径而非 URL：URL 含 ?owner= 且域名可变，存路径更稳，与 Content.images.path 同构。
    qrcode_path: Mapped[str] = mapped_column(String(300), default="")

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 上次连通性验证成功的时间戳（系统状态页展示「已验证 / 未验证」）
    verified_at: Mapped[int | None] = mapped_column(nullable=True)

    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)


class AppSetting(Base):
    """通用应用设置（owner + key 唯一）。

    为什么单开一张键值表，而不是往 wechat_accounts 加列：
      作者信息不属于某个公众号，加列会让语义错乱（多号时还歧义）。
      键值表能承载「一次配置、全局复用」的各种设置，后续加项零成本。

    value 统一存字符串（需要结构化时由调用方自行 JSON 编解码），保持表简单。
    """

    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    key: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[int] = mapped_column(default=_now)

    # owner + key 唯一：同一归属下一篇设置只存一条，upsert 语义天然成立
    __table_args__ = (Index("ix_app_settings_owner_key", "owner", "key", unique=True),)
