"""免费模型怎么记账。

这条线以前是错的：免费模型（CF / 火山免费档）没登记单价，于是被兜底成
DeepSeek Flash 的价，账上凭空多出一笔钱 —— 数量不大，但方向很坏：
用免费模型反而显示花钱，会让人不敢用免费模型。

修完之后要同时守住两头：
  - **已知免费 → 0 元**，且不标「按兜底价估算」的 ⚠；
  - **不知道多少钱 → 仍然按兜底价估**，绝不能因为「没登记」就记成免费
    （账目偏低的后果比偏高严重得多）。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.core import pricing
from app.db.session import Base
from app.services import model_pool, usage


def _mk(tmp_path, name="billing.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}")
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_pricing():
    """每个用例都从干净的内置单价表出发，避免互相污染。"""
    pricing.reload_pricing()
    yield
    pricing.reload_pricing()


# ---------------------------------------------------------------- 1. 单价表侧

def test_free_flag_costs_zero():
    pricing.register_model(
        "@cf/free-llm", {"label": "CF 免费模型", "vendor": "cloudflare", "free": True, "in": 0, "out": 0}
    )
    b = pricing.cost_breakdown("@cf/free-llm", 10_000, 2_000)
    assert b["billing"] == "free"
    assert b["known"] is True           # 免费是「已知」，不是「没登记」
    assert b["cost"] == 0.0
    assert b["peak_multiplier"] == 1.0  # 免费模型不谈峰谷


def test_all_zero_prices_mean_free():
    pricing.register_model("zero-model", {"in": 0, "out": 0, "in_cached": 0})
    assert pricing.is_free("zero-model") is True
    assert pricing.cost_breakdown("zero-model", 5_000, 1_000)["cost"] == 0.0


def test_unknown_model_still_falls_back_not_free():
    """没登记过的模型：按兜底价估，且明确标 unknown —— 不能算成免费。"""
    b = pricing.cost_breakdown("some-brand-new-model", 10_000, 2_000)
    assert b["billing"] == "unknown"
    assert b["known"] is False
    assert b["cost"] > 0  # 兜底价，不是 0


def test_no_price_at_all_is_not_free():
    """一条价都没填 ≠ 免费。填了 label 没填价是很常见的疏漏，别当成 0 元。"""
    pricing.register_model("label-only", {"label": "只有名字"})
    assert pricing.is_free("label-only") is False
    assert usage.billing_of("label-only") == "unknown"


# ---------------------------------------------------------------- 2. 模型池侧

def test_pool_free_model_is_free_even_without_price_entry():
    """池里标了 free 就是 free —— 池是免费模型的权威来源。

    这才是真实场景：新接入一个免费模型，池里 billing=free，
    但没人会第一时间去单价表补一条。
    """
    model_pool._snapshot = model_pool.PoolSnapshot(
        catalog={
            "c1": {"id": "c1", "model_id": "@cf/zai-org/glm-4.7-flash", "billing": "free"},
        }
    )
    try:
        assert usage.billing_of("@cf/zai-org/glm-4.7-flash") == "free"
    finally:
        model_pool._snapshot = None


def test_pool_paid_without_price_is_unknown():
    """池里标 paid 但单价表没价 → unknown，按兜底价估，不冒充免费。"""
    model_pool._snapshot = model_pool.PoolSnapshot(
        catalog={"c1": {"id": "c1", "model_id": "glm-4-plus", "billing": "paid"}}
    )
    try:
        assert usage.billing_of("glm-4-plus") == "unknown"
    finally:
        model_pool._snapshot = None


def test_provider_default_free_covers_unlisted_model():
    """模型没进池，但它家是免费通道（CF 设置页下拉可以直接选）→ 也算免费。

    这是最容易漏的一档：模型池里查不到这个模型名，只看模型级就判成
    「不知道多少钱」，于是按兜底价记了一笔并不存在的钱。
    """
    model_pool._snapshot = model_pool.PoolSnapshot(
        providers={"p1": {"key": "cloudflare", "billing_default": "free"}},
        catalog={"c1": {"id": "c1", "model_id": "@cf/known-one", "billing": "free"}},
    )
    try:
        assert usage.billing_of("@cf/not-in-pool", "cloudflare") == "free"
        # 不带 provider_key 就认不出来 —— 所以调用方一定得把服务商传进来
        assert usage.billing_of("@cf/not-in-pool") == "unknown"
    finally:
        model_pool._snapshot = None


def test_paid_provider_default_does_not_make_things_free():
    model_pool._snapshot = model_pool.PoolSnapshot(
        providers={"p1": {"key": "zhipu", "billing_default": "paid"}},
    )
    try:
        assert usage.billing_of("glm-4-plus", "zhipu") == "unknown"
    finally:
        model_pool._snapshot = None


def test_migrate_free_providers_flips_cloudflare(tmp_path):
    """老库加列时补的是 'paid'，Cloudflare 那行必须被迁移成 'free'。"""
    from app.models.model_pool import ModelProvider

    async def _case():
        engine, Session = _mk(tmp_path, "migrate.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(ModelProvider(owner="system", key="cloudflare", name="CF", kind="cloudflare",
                                 base_url="https://x", api_key="t", billing_default="paid"))
            db.add(ModelProvider(owner="system", key="zhipu", name="智谱", kind="openai",
                                 base_url="https://y", api_key="k", billing_default="paid"))
            await db.commit()

            n = await model_pool.migrate_free_providers(db)
            await db.commit()
            assert n == 1

            rows = (
                await db.execute(
                    __import__("sqlalchemy").select(ModelProvider).order_by(ModelProvider.key)
                )
            ).scalars().all()
            got = {r.key: r.billing_default for r in rows}
            assert got["cloudflare"] == "free"
            assert got["zhipu"] == "paid"  # 正常付费厂商不受影响
        await engine.dispose()

    _run(_case())


def test_paid_model_in_pool_with_price_is_paid():
    pricing.register_model("glm-4-plus", {"in": 1.0, "out": 1.0})
    model_pool._snapshot = model_pool.PoolSnapshot(
        catalog={"c1": {"id": "c1", "model_id": "glm-4-plus", "billing": "paid"}}
    )
    try:
        assert usage.billing_of("glm-4-plus") == "paid"
    finally:
        model_pool._snapshot = None


# ---------------------------------------------------------------- 3. 落库与定格

def test_record_freezes_billing_and_costs_zero(tmp_path):
    async def _case():
        engine, Session = _mk(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await usage.record(
                db, model="@cf/free-llm", prompt_tokens=10_000, completion_tokens=2_000,
                billing="free", owner="u1",
            )
            await usage.record(
                db, model="deepseek-flash", prompt_tokens=10_000, completion_tokens=2_000,
                owner="u1",
            )
            await db.commit()

            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            by_model = {r.model: r for r in rows}
            free_row = by_model["@cf/free-llm"]
            paid_row = by_model["deepseek-flash"]

            assert free_row.billing == "free"
            assert usage.cost_of(free_row) == 0.0
            assert paid_row.billing == "paid"
            assert usage.cost_of(paid_row) > 0

            agg = usage._agg(rows)
            assert agg["free_calls"] == 1
            assert agg["free_tokens"] == 12_000
            # 免费那笔一分钱不加，合计只剩付费那笔
            assert agg["cost"] == pytest.approx(usage.cost_of(paid_row), abs=1e-6)
        await engine.dispose()

    _run(_case())


def test_billing_is_frozen_even_if_model_later_becomes_paid(tmp_path):
    """定格的意义：事后把模型改成付费，历史那笔不该跟着涨价。"""
    async def _case():
        engine, Session = _mk(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await usage.record(
                db, model="was-free", prompt_tokens=1_000, completion_tokens=1_000,
                billing="free", owner="u1", created_at=int(time.time()),
            )
            await db.commit()

        # 事后：这个模型改成付费并登记了单价
        pricing.register_model("was-free", {"in": 10.0, "out": 40.0})

        async with Session() as db:
            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            assert rows[0].billing == "free"
            assert usage.cost_of(rows[0]) == 0.0
        await engine.dispose()

    _run(_case())


def test_legacy_rows_without_billing_still_cost_money(tmp_path):
    """加列之前的老行 billing 是空串 —— 维持原口径（按兜底价算），不回溯清零。"""
    async def _case():
        engine, Session = _mk(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await usage.record(
                db, model="deepseek-flash", prompt_tokens=1_000, completion_tokens=1_000,
                owner="u1",
            )
            # 手工模拟老数据：没有 billing
            row = (await db.execute(usage._base_query(owner="u1"))).scalars().first()
            row.billing = ""
            await db.commit()
            assert usage.cost_of(row) > 0
        await engine.dispose()

    _run(_case())


def test_row_out_exposes_billing(tmp_path):
    async def _case():
        engine, Session = _mk(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await usage.record(
                db, model="@cf/free-llm", prompt_tokens=100, completion_tokens=100,
                billing="free", owner="u1",
            )
            await usage.record(
                db, model="never-heard-of-it", prompt_tokens=100, completion_tokens=100,
                owner="u1",
            )
            await db.commit()
            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            outs = [usage._row_out(r) for r in rows]
            got = {o["model"]: o for o in outs}
            assert got["@cf/free-llm"]["billing"] == "free"
            assert got["@cf/free-llm"]["cost"] == 0.0
            assert got["never-heard-of-it"]["billing"] == "unknown"
            assert got["never-heard-of-it"]["cost"] > 0
        await engine.dispose()

    _run(_case())


def test_backfill_marks_old_free_calls(tmp_path):
    """加列之前的老行：能证明当时是免费模型的，把 billing 补成 free。

    补不了的不动（保持空串 = 按兜底价估），也不改任何金额 ——
    这里只是把「免费」这个事实补记上去。
    """
    from app.models.model_pool import ModelCatalog, ModelProvider

    async def _case():
        engine, Session = _mk(tmp_path, "backfill.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            db.add(ModelProvider(owner="system", key="cloudflare", name="CF",
                                 kind="cloudflare", base_url="https://x", api_key="t"))
            db.add(ModelCatalog(provider_key="cloudflare", model_id="@cf/known-one",
                                label="已知免费", billing="free", capabilities=["chat"]))
            await db.commit()

            await usage.record(db, model="@cf/known-one", prompt_tokens=100,
                               completion_tokens=100, owner="u1")
            await usage.record(db, model="deepseek-flash", prompt_tokens=100,
                               completion_tokens=100, owner="u1")
            # 手工清掉 billing，模拟加列之前的老数据
            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            for r in rows:
                r.billing = ""
            await db.commit()

            await model_pool.refresh_snapshot(db)

            n = await usage.backfill_free_billing(db)
            await db.commit()
            assert n == 1

            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            got = {r.model: r.billing for r in rows}
            assert got["@cf/known-one"] == "free"
            assert got["deepseek-flash"] == ""  # 付费模型不受影响
            assert usage.cost_of([r for r in rows if r.model == "@cf/known-one"][0]) == 0.0
        await engine.dispose()

    _run(_case())


def test_backfill_consults_pricing_and_fixes_unknown(tmp_path):
    """免费模型只在 PRICING 登记（没进池）、且历史行存成 unknown 时：
    仍要被认成免费并订正 —— 否则 warning 一直挂着、费用被兜底价估成钱。
    """
    pricing.register_model(
        "@cf/zai-org/glm-4.7-flash",
        {"label": "GLM-4.7-Flash", "vendor": "cloudflare", "in": 0, "out": 0, "free": True},
    )

    async def _case():
        engine, Session = _mk(tmp_path, "backfill2.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await usage.record(
                db, model="@cf/zai-org/glm-4.7-flash",
                prompt_tokens=100, completion_tokens=100, owner="u1",
            )
            # 模拟历史：当初没登记单价，落成了 unknown
            row = (await db.execute(usage._base_query(owner="u1"))).scalars().first()
            row.billing = "unknown"
            await db.commit()

            n = await usage.backfill_free_billing(db)
            await db.commit()
            assert n == 1

            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            assert rows[0].billing == "free"
            assert usage.cost_of(rows[0]) == 0.0
        await engine.dispose()

    _run(_case())


def test_sink_payload_billing_reaches_row(tmp_path):
    """走埋点回调（真实链路）时，billing 也要落到行上。"""
    async def _case():
        engine, Session = _mk(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            sink = usage.make_sink(db, owner="u1", scene="chat")
            await sink({
                "model": "@cf/zai-org/glm-4.7-flash",
                "billing": "free",
                "cred_owner": "system",
                "prompt_tokens": 3_000,
                "completion_tokens": 1_000,
            })
            rows = (await db.execute(usage._base_query(owner="u1"))).scalars().all()
            assert len(rows) == 1
            assert rows[0].billing == "free"
            assert usage.cost_of(rows[0]) == 0.0
        await engine.dispose()

    _run(_case())
