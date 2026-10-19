"""知乎热点存档。

三张表，职责分明：

| 表 | 一行代表 | 为什么这样分 |
|---|---|---|
| `HotSnapshot` | 一次抓取批次 | 审计用：哪次成功、哪次失败、抓到几条。定时任务不报警，唯一的凭据就是它 |
| `HotTopic` | 某话题在某次抓取中的**一次露面** | 刻意不做「话题主表 + 排名从表」——见下方说明 |
| `ZhihuApiUsage` | 某天的 API 调用次数 | 额度水位保护的计数源 |

为什么 HotTopic 是「一次露面」而不是「话题主表」
------------------------------------------
直觉做法是一张话题主表，每次抓取 UPSERT 更新 rank。但那样会丢掉历史
序列，而**趋势恰恰是这个模块唯一值钱的东西**（平台返回里没有热度值字段，
只有名次，见 services/zhihu.py 顶部注释）——“2 小时从第 28 名冲到第 6 名”
这种信号，必须靠同一话题在不同时刻的多条 rank 记录才能算出来。

行数估算：30 条 × 48 次/天 = 1440 行/天，一年约 53 万行。SQLite 无压力，
查询侧用 `(question_id, fetched_at)` 索引兜住。
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> int:
    return int(time.time())


class HotSnapshot(Base):
    """一次热榜抓取批次（无论成败都记，漏采只能靠它发现）。"""

    __tablename__ = "zhihu_hot_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    fetched_at: Mapped[int] = mapped_column(Integer, index=True)
    # 成功时 = 入库条目数；失败时 = 0
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # 失败原因 / 跳过原因（如「已达当日额度上限」）
    error: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    # 本次实际调用 API 的次数（额度保护要精确计数）
    api_calls: Mapped[int] = mapped_column(Integer, default=0)


class HotTopic(Base):
    """某话题在某次抓取里的露面（含当时名次）。"""

    __tablename__ = "zhihu_hot_topics"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uid)
    # 从 Url 里解析出的问题/文章 id —— 跨批次归并同一话题的唯一稳定键。
    # ⚠️ 不能用完整 Url 做 key：它带 utm_source 等追踪参数，且每个
    # Access Secret 的 utm_source 还不一样。
    question_id: Mapped[str] = mapped_column(String(64), index=True)
    fetched_at: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[str] = mapped_column(String(32), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512))
    # 清洗掉 utm_* 后的干净链接（给用户看/引用用）
    url: Mapped[str] = mapped_column(String(512))
    # 平台自带的 ~200 字摘要：选题判断零成本素材，不用再调任何接口
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # 免费封面图（知乎图床）—— 存在不意味着可直接商用，见 docs 方案的版权节
    thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True, default=None)

    __table_args__ = (Index("ix_zhihu_hot_qid_time", "question_id", "fetched_at"),)


class ZhihuApiUsage(Base):
    """每日 API 调用计数（按 CST 自然日，见 services/zhihu.py 的 _today_key）。
    也扣），一旦烧穿，后半程连实时查询都没有。定时抓取必须在接近上限时
    主动让路 —— 宁可少抓一次，不能把池子用光。
    """

    __tablename__ = "zhihu_api_usage"

    # CST 日期字符串 '2026-10-19'
    day: Mapped[str] = mapped_column(String(16), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, default=_now)
