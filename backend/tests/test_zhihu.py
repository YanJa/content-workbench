"""知乎热点模块测试。

四条主线：

1. **纯函数**：question_id 解析 / utm 清洗 / 屏蔽词过滤 —— 不碰网络不碰库；
2. **趋势计算**：这是整个模块的价值所在（平台不给热度值，只能自己算排名
   变化）。构造两个批次的假数据验证 climb / is_new / staying_hours /
   heat_score；
3. **额度纪律**：水位到线后定时抓取必须让路；`force=True` 例外（手动刷新）；
4. **抓取审计**：失败也要留下 ok=False 的批次记录 —— 定时任务不报警，漏采
   只能靠这张表发现。

网络隔离
--------
所有会把 HTTP 打出去的测试都 monkeypatch 掉 `zhihu.fetch_raw_hot`，
**绝不在这套测试里消耗真实额度**（那是用户账号下的共享池，用完就没了）。

每个测试一套独立临时库（NullPool），不碰开发库。注意：本模块在文件顶部
`from app.db.session import SessionLocal`，是一份模块级引用，fixture 必须
patch `zhihu_mod.SessionLocal`，漏了就会写进开发库（test_perm_quota.py 的
fixture 注释里踩过同样的坑）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.db.session import Base, get_db
from app.main import app
from app.models.zhihu import HotSnapshot, HotTopic, ZhihuApiUsage
from app.services import zhihu as zhihu_mod

CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "test_zhihu.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    async def _override_get_db():
        async with Maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    # ⚠️ 关键：本模块持有 SessionLocal 的模块级引用，必须在这里换掉
    monkeypatch.setattr(zhihu_mod, "SessionLocal", Maker)
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_ACCESS_SECRET", "test-secret")
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_BLOCK_KEYWORDS", "")
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_DAILY_QUOTA", 100)
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_QUOTA_SAFETY", 88)
    yield Maker
    app.dependency_overrides.clear()


def _hot_item(title: str, qid: str, summary: str = "摘要") -> dict:
    """伪造一条平台返回 Item（结构对齐实跑结果）。"""
    return {
        "Title": title,
        "Url": f"https://www.zhihu.com/question/{qid}?utm_medium=openapi_platform&utm_source=abc123",
        "ThumbnailUrl": f"https://pic.zhimg.com/{qid}.jpg",
        "Summary": summary,
    }


async def _seed_pair(Maker, now: int):
    """造两个批次：两天前一批、现在一批，用于验证趋势。

    话题 A：两天前第 20 名 → 现在第 4 名（climb=+16，猛蹿）
    话题 B：两天前第 5 名  → 现在第 8 名（climb=-3，下滑）
    话题 C：只在最新批次出现（今天首次上榜）
    """
    old_at, new_at = now - 2 * 86400, now
    async with Maker() as s:
        s.add(HotSnapshot(id="s_old", fetched_at=old_at, item_count=2, ok=True, api_calls=1))
        s.add(HotSnapshot(id="s_new", fetched_at=new_at, item_count=3, ok=True, api_calls=1))
        s.add(
            HotTopic(
                id="t1", snapshot_id="s_old", question_id="question_A", rank=20,
                fetched_at=old_at, title="话题A", url="https://www.zhihu.com/question/A",
            )
        )
        s.add(
            HotTopic(
                id="t2", snapshot_id="s_old", question_id="question_B", rank=5,
                fetched_at=old_at, title="话题B", url="https://www.zhihu.com/question/B",
            )
        )
        s.add(
            HotTopic(
                id="t3", snapshot_id="s_new", question_id="question_A", rank=4,
                fetched_at=new_at, title="话题A", url="https://www.zhihu.com/question/A",
            )
        )
        s.add(
            HotTopic(
                id="t4", snapshot_id="s_new", question_id="question_B", rank=8,
                fetched_at=new_at, title="话题B", url="https://www.zhihu.com/question/B",
            )
        )
        s.add(
            HotTopic(
                id="t5", snapshot_id="s_new", question_id="question_C", rank=1,
                fetched_at=new_at, title="话题C", url="https://www.zhihu.com/question/C",
            )
        )
        await s.commit()


# ------------------------------------------------------------------ 1 纯函数
def test_parse_question_id_strips_utm_and_tracks_stable_id():
    qid = zhihu_mod.parse_question_id(
        "https://www.zhihu.com/question/2094350166518249285?utm_medium=openapi_platform&utm_source=cf621feb3f2d"
    )
    # 带祖：utm_source 跟 secret 绑定，不同 secret 值不同 —— 必须剥掉只用数字 id
    assert qid == "question_2094350166518249285"

    # 专栏文章 p/<id> 与问答 question/<id> 前缀不同，不能丢前缀否则可能撞号
    assert zhihu_mod.parse_question_id("https://zhuanlan.zhihu.com/p/2089828274960602968?x=1") == "p_2089828274960602968"


def test_clean_url_drops_tracking_params():
    assert (
        zhihu_mod.clean_url("https://www.zhihu.com/question/123?utm_medium=openapi_platform&utm_source=x")
        == "https://www.zhihu.com/question/123"
    )


def test_block_keywords_filters_by_title_and_summary(monkeypatch):
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_BLOCK_KEYWORDS", "选举，战争 trump")
    # 命中标题
    assert zhihu_mod.is_blocked("某地选举结果出炉", "")
    # 命中摘要也算（有些热点标题不含敏感词但摘要有）
    assert zhihu_mod.is_blocked("如何看待这件事", "涉及贸易战争")
    # 中英文逗号混写都能切
    assert zhihu_mod.is_blocked("trump says", "")
    # 未命中
    assert not zhihu_mod.is_blocked("iPhone 降价", "价格回落")


# ------------------------------------------------------------------ 2 趋势
def test_hot_with_trend_computes_climb_and_new(env):
    now = int(time.time())
    asyncio.run(_seed_pair(env, now))
    res = asyncio.run(zhihu_mod.hot_with_trend(limit=10))

    assert res["ok"], res
    items = {i["question_id"]: i for i in res["items"]}
    a, b, c = items["question_A"], items["question_B"], items["question_C"]

    # 20 → 4：正数是往上冲
    assert a["climb"] == 16
    # 5 → 8：负数是下滑
    assert b["climb"] == -3
    # 首次进榜在两天前 → 不是今天新上榜
    assert a["is_new"] is False
    assert a["staying_hours"] == pytest.approx(48.0, abs=0.2)
    # C 只在最新批次露过面 → 今天新上榜
    assert c["is_new"] is True
    # A 出现过两次
    assert a["seen_times"] == 2 and c["seen_times"] == 1
    # 在爬升的权重高于名次：A（+16 但第4）应排在 C（+0 但第1）前面
    assert a["heat_score"] > c["heat_score"] > b["heat_score"]
    # url 必须是清洗过的（不能有 utm）
    assert "utm_" not in a["url"]


def test_hot_with_trend_empty_before_first_fetch(env):
    res = asyncio.run(zhihu_mod.hot_with_trend(limit=10))
    # 一次都没抓过时不能炸，要有可读错误给前端
    assert res["ok"] is False and "还没有任何成功抓取" in res["error"]


# ------------------------------------------------------------------ 3 额度纪律
def test_quota_water_level_stops_scheduled_fetch(env, monkeypatch):
    async def _never_called(*_a, **_kw):  # pragma: no cover - 真被调用就是 bug
        raise AssertionError("已达水位就不该再发 HTTP 请求")

    monkeypatch.setattr(zhihu_mod, "fetch_raw_hot", _never_called)
    now = int(time.time())

    async def _seed_usage():
        async with env() as s:
            s.add(ZhihuApiUsage(day=zhihu_mod._today_key(), count=90, updated_at=now))
            await s.commit()

    asyncio.run(_seed_usage())

    res = asyncio.run(zhihu_mod.fetch_hot_once())
    assert res["ok"] is False and res.get("skipped") is True
    assert "水位" in res["error"]

    # 跳过也要留下审计记录 —— 否则事后无法区分「没跑」和「跑了没数据」
    async def _count():
        async with env() as s:
            return (
                await s.execute(select(HotSnapshot).where(HotSnapshot.ok.is_(False)))
            ).scalars().all()

    snaps = asyncio.run(_count())
    assert snaps and "水位" in (snaps[-1].error or "")


def test_force_refresh_bypasses_water_level(env, monkeypatch):
    called = {"n": 0}

    async def _fake_fetch_raw(limit=None):
        called["n"] += 1
        return {"ok": True, "items": [_hot_item("标题1", "111"), _hot_item("标题2", "222")]}

    monkeypatch.setattr(zhihu_mod, "fetch_raw_hot", _fake_fetch_raw)
    now = int(time.time())

    async def _seed_usage():
        async with env() as s:
            s.add(ZhihuApiUsage(day=zhihu_mod._today_key(), count=95, updated_at=now))
            await s.commit()

    asyncio.run(_seed_usage())

    res = asyncio.run(zhihu_mod.fetch_hot_once(force=True))
    assert res["ok"] and res["saved"] == 2
    assert called["n"] == 1


# ------------------------------------------------------------------ 4 抓取与审计
def test_fetch_saves_topics_and_counts_usage(env, monkeypatch):
    async def _fake_fetch_raw(limit=None):
        return {
            "ok": True,
            "items": [
                _hot_item("正常话题", "1001", "摘要1"),
                _hot_item("屏蔽词话题", "1002", "含敏感词"),
            ],
        }

    monkeypatch.setattr(zhihu_mod, "fetch_raw_hot", _fake_fetch_raw)
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_BLOCK_KEYWORDS", "敏感词")

    res = asyncio.run(zhihu_mod.fetch_hot_once())
    assert res["ok"] and res["saved"] == 1 and res["blocked"] == 1

    async def _check():
        async with env() as s:
            topics = (await s.execute(select(HotTopic))).scalars().all()
            usage = (await s.execute(select(ZhihuApiUsage))).scalar_one_or_none()
            snap = (
                await s.execute(select(HotSnapshot).where(HotSnapshot.ok.is_(True)))
            ).scalar_one_or_none()
            return topics, usage, snap

    topics, usage, snap = asyncio.run(_check())
    assert len(topics) == 1 and topics[0].question_id == "question_1001"
    assert topics[0].rank == 1
    # 从 Ulr 里剥了 utm 才入库
    assert topics[0].url == "https://www.zhihu.com/question/1001"
    assert usage is not None and usage.count == 1
    assert snap is not None and snap.item_count == 1


def test_failed_fetch_leaves_audit_row(env, monkeypatch):
    async def _fake_fetch_raw_fail(limit=None):
        return {"ok": False, "error": "知乎返回 Code=30001：触发限流"}

    monkeypatch.setattr(zhihu_mod, "fetch_raw_hot", _fake_fetch_raw_fail)

    res = asyncio.run(zhihu_mod.fetch_hot_once())
    assert res["ok"] is False and "限流" in res["error"]

    async def _check():
        async with env() as s:
            return (
                await s.execute(select(HotSnapshot).where(HotSnapshot.ok.is_(False)))
            ).scalar_one_or_none()

    snap = asyncio.run(_check())
    assert snap is not None and "限流" in snap.error
    # 失败也算一次调用（平台侧口径我们说了不算，保守计数）
    assert snap.api_calls == 1


def test_unconfigured_secret_no_http_call(env, monkeypatch):
    monkeypatch.setattr(zhihu_mod.settings, "ZHIHU_ACCESS_SECRET", "")

    async def _never(*_a, **_kw):  # pragma: no cover
        raise AssertionError("没配凭证就不该发请求")

    monkeypatch.setattr(zhihu_mod, "fetch_raw_hot", _never)
    res = asyncio.run(zhihu_mod.fetch_hot_once())
    assert res["ok"] is False and res.get("skipped") is True
