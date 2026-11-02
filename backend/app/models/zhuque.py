"""朱雀 AI 检测用量估算（仅本应用视角的月度聚合）。

为什么单独一张表、而非往 messages / llm_usage 加字段：
  朱雀走的是 EdgeOne Makers 的独立模型（@makers/zhuque-text），不进 LLM 调用链，
  也没有 message 归属，强行并入 llm_usage 反而语义错乱。它就是一个「本月花了多少
  Makers token」的计数，独立一张小表最干净。

⚠️ 非权威：本表只统计「本应用、本 owner、用这把 ZHUQUE_API_KEY 发起的检测」，
  算不到别的 App / 别的用户用同一把 key 的消耗，也不反映月切重置、密钥轮换。
  权威剩余额度以 EdgeOne 控制台 Makers → 用量/账单 为准。界面必须带这个免责说明。
"""
from __future__ import annotations

import time

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _now() -> int:
    return int(time.time())


class ZhuqueUsage(Base):
    """某 owner 某月的朱雀 token 用量估算（月切自动开新行）。"""

    __tablename__ = "zhuque_usage"

    # (owner, month) 唯一；month = 'YYYY-MM'
    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(8), primary_key=True)
    # 本月累计消耗的 Makers token 数（来自各次检测响应的 makers_models_usage.total_tokens）
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    # 本月累计检测次数（仅供观察调用频次）
    calls: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)
