"""流量主广告日数据（收益看板数据源）。

为什么落库而不是每次现拉：
  1. 微信广告分析接口按 90 天最大跨度分页返回，每次整段重拉既慢又易触发限频；
  2. 数据 T+1 才出，当天拉和昨天拉结果一样，没必要每次打接口；
  3. 看板要画「趋势 / 收益构成」，需要稳定的历史序列，落库最稳。

owner 隔离：每行都带 owner（= 公众号账号的 owner），与系统其它业务表口径一致。
唯一键 (owner, account_id, date, ad_slot)：同一天同一广告位只存一条，upsert 语义天然成立。
所有金额按「分」入库（income_fen / ecpm_fen），前端换算成元展示，避免浮点漂移。
"""
from __future__ import annotations

import time

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class WechatAdDaily(Base):
    """公众号 / 小程序广告的日级天级数据（来自 publisher_adpos_general）。"""

    __tablename__ = "wechat_ad_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    owner: Mapped[str] = mapped_column(String(64), default="default", index=True)
    account_id: Mapped[str] = mapped_column(String(12), index=True)  # 对应 wechat_accounts.id

    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    slot_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 微信广告位 id（唯一维度）
    ad_slot: Mapped[str] = mapped_column(String(40), default="")  # 原始枚举，如 SLOT_ID_BIZ_BOTTOM（部分行可能为空）
    ad_slot_name: Mapped[str] = mapped_column(String(40), default="")  # 中文名，如「底部广告」

    req_succ_count: Mapped[int] = mapped_column(Integer, default=0)  # 拉取量
    exposure_count: Mapped[int] = mapped_column(Integer, default=0)  # 曝光量
    exposure_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 曝光率
    click_count: Mapped[int] = mapped_column(Integer, default=0)  # 点击量
    click_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 点击率
    income_fen: Mapped[int] = mapped_column(Integer, default=0)  # 收入（分）
    ecpm_fen: Mapped[int] = mapped_column(Integer, default=0)  # 千次曝光收益（分）

    created_at: Mapped[int] = mapped_column(Integer, default=_now)

    # 微信接口里 (date, slot_id) 才是唯一维度——部分行的 ad_slot 名为空，
    # 但 slot_id 各不相同，不能拿 ad_slot 当唯一键（否则同天多条未命名行会被合并，少算）。
    __table_args__ = (
        Index("ix_ad_daily_uniq", "owner", "account_id", "date", "slot_id", unique=True),
    )
