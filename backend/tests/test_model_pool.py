"""模型池解析层的单元测试。

重点验证「改完之后不会比改之前更差」这条底线：
  1. 池是空的 → 解析不到 → 调用方回落环境变量通道（不能因为加了池就瘫痪）；
  2. CF 的两个特例（不传 tools、不认 stream_options）从字符串判断改成 flag 之后，
     老路径行为一字不变；
  3. 额度耗尽 / 熔断 / 凭证缺失 → 自动跳到下一个候选；
  4. 个人 Key 覆盖 → 烧的是自己的额度（cred_owner = 用户 id）。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  建表需要
from app.db.session import Base
from app.models.model_pool import (
    ModelBinding,
    ModelCatalog,
    ModelProvider,
    ModelProviderKey,
)
from app.services import model_pool


def _mk(tmp_path, name="pool.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}")
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _init(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _provider(**kw) -> ModelProvider:
    base = dict(
        owner="system", key="zhipu", name="智谱", kind="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4", api_key="sk-test", enabled=True,
    )
    base.update(kw)
    return ModelProvider(**base)


def _catalog(**kw) -> ModelCatalog:
    base = dict(
        provider_key="zhipu", model_id="glm-4-flash", label="GLM-4-Flash",
        capabilities=["chat"], protocol="openai_chat", enabled=True, sort=10,
    )
    base.update(kw)
    return ModelCatalog(**base)


# ---------------------------------------------------------------- 1. 空池回落

async def _empty_pool(tmp_path):
    engine, Session = _mk(tmp_path)
    await _init(engine)
    async with Session() as db:
        await model_pool.refresh_snapshot(db)
    return engine, Session


def test_empty_pool_falls_back_to_env(tmp_path):
    async def _run():
        await _empty_pool(tmp_path)
        # 池里什么都没有 —— 解析必须返回 None，调用方回落环境变量通道
        assert model_pool.resolve_model("chat") is None
        assert model_pool.resolve_candidates("chat") == []
        model_pool.invalidate()

    asyncio.run(_run())


def test_feature_without_binding_returns_none(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider())
            db.add(_catalog())
            await db.commit()
            await model_pool.refresh_snapshot(db)
        # 有模型但没有绑定 → 仍然走老路
        assert model_pool.resolve_model("chat") is None
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 2. 正常解析

def test_resolve_returns_bound_model(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            p = _provider()
            db.add(p)
            c = _catalog()
            db.add(c)
            await db.flush()
            db.add(ModelBinding(feature="chat", primary_model_id=c.id, fallbacks=[]))
            db.add(ModelBinding(feature="layout", primary_model_id=c.id, fallbacks=[]))
            await db.commit()
            await model_pool.refresh_snapshot(db)

        spec = model_pool.resolve_model("chat")
        assert spec is not None
        assert spec.model == "glm-4-flash"
        assert spec.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert spec.api_key == "sk-test"
        assert spec.cred_owner == "system"
        assert spec.supports_tools is True
        # 条目没配参数 → 保持 None，让调用方沿用 MODEL_ROUTES 里调过的值
        assert spec.temperature is None and spec.max_tokens is None
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 3. 旧 CF 行为不变

def test_legacy_spec_keeps_cloudflare_flags():
    """CF 的特例从字符串判断变成 flag；stream_usage 仍 False，但 supports_tools 已放开。

    2026-10-30 修正：CF 对话模型（@cf/meta/llama-3.x、@cf/zai-org/glm-4.x）实测
    已支持 function calling，不再默认 False —— 否则直接配 CLOUDFLARE_CHAT_MODEL、
    没进模型池的对话会整组丢失工具能力。不支持的个别 CF 模型由 llm 层兜底忽略。
    """
    from app.services.llm import _legacy_spec

    cf = _legacy_spec("cloudflare", None)
    assert cf.supports_tools is True, "CF 对话模型实测支持 function calling"
    assert cf.supports_stream_usage is False, "CF 的 /ai/v1 不认 stream_options（带了 400）"
    assert cf.max_tokens == 4096 and cf.temperature == 0.7
    assert cf.provider_key == "cloudflare"

    ds = _legacy_spec("deepseek", None)
    assert ds.supports_tools is True and ds.supports_stream_usage is True
    assert ds.provider_key == "deepseek"
    assert ds.model == "", "不传 override 时留空，由调用方用 MODEL_ROUTES 填"


# ---------------------------------------------------------------- 4. 跳过不健康的

def test_skips_disabled_and_no_key_and_circuit_broken(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider(key="p1", api_key=""))  # 没 Key
            db.add(_provider(key="p2", enabled=False))  # 停用
            db.add(_provider(key="p3"))
            bad = _catalog(provider_key="p1", model_id="m-nokey")
            off = _catalog(provider_key="p2", model_id="m-off")
            boom = _catalog(
                provider_key="p3", model_id="m-boom", disabled_until=int(time.time()) + 300
            )
            ok = _catalog(provider_key="p3", model_id="m-ok")
            for c in (bad, off, boom, ok):
                db.add(c)
            await db.flush()
            db.add(
                ModelBinding(
                    feature="chat",
                    primary_model_id=bad.id,
                    fallbacks=[off.id, boom.id, ok.id],
                )
            )
            await db.commit()
            await model_pool.refresh_snapshot(db)

        spec = model_pool.resolve_model("chat")
        assert spec is not None and spec.model == "m-ok"
        model_pool.invalidate()

    asyncio.run(_run())


def test_daily_calls_limit_exhausted_skips_model(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider())
            full = _catalog(model_id="m-full", daily_calls_limit=1)
            spare = _catalog(model_id="m-spare")
            db.add_all([full, spare])
            await db.flush()
            db.add(
                ModelBinding(feature="chat", primary_model_id=full.id, fallbacks=[spare.id])
            )
            await db.commit()
            await model_pool.refresh_snapshot(db)

            # 还没用 → 用主模型
            assert model_pool.resolve_model("chat").model == "m-full"
            # 记一笔 → 主模型当日额度用尽 → 自动跳到备用
            spec = model_pool.resolve_model("chat")
            await model_pool.bump_quota(spec, tokens=100, session=db)
            await model_pool.refresh_snapshot(db)
            assert model_pool.resolve_model("chat").model == "m-spare"
        model_pool.invalidate()

    asyncio.run(_run())


def test_provider_scope_quota_is_shared_across_models(tmp_path):
    """CF 的免费额度是账号级：一个模型用完了，同账号其它模型也该被跳过。"""
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider(key="cloudflare", kind="cloudflare", quota_reset_tz="utc"))
            a = _catalog(
                provider_key="cloudflare", model_id="@cf/a", quota_scope="provider"
            )
            b = _catalog(
                provider_key="cloudflare", model_id="@cf/b", quota_scope="provider"
            )
            db.add_all([a, b])
            await db.flush()
            db.add(ModelBinding(feature="chat", primary_model_id=a.id, fallbacks=[b.id]))
            await db.commit()
            await model_pool.refresh_snapshot(db)

            # 给账号级桶塞满（provider 的 daily_calls_limit=2）
            await db.execute(
                select(ModelProvider).where(ModelProvider.key == "cloudflare")
            )
            prov = (
                await db.execute(
                    select(ModelProvider).where(ModelProvider.key == "cloudflare")
                )
            ).scalar_one()
            prov.daily_calls_limit = 1
            await db.commit()
            await model_pool.refresh_snapshot(db)

            spec = model_pool.resolve_model("chat")
            assert spec.model == "@cf/a"
            await model_pool.bump_quota(spec, tokens=10, session=db)
            await model_pool.refresh_snapshot(db)
            # 桶是共享的 → B 也不可用（不是「每个模型各有 1 次」）
            assert model_pool.resolve_model("chat") is None
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 5. 个人 Key

def test_personal_key_overrides_and_owns_quota(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider(api_key="sk-global"))
            c = _catalog()
            db.add(c)
            await db.flush()
            db.add(ModelBinding(feature="chat", primary_model_id=c.id, fallbacks=[]))
            db.add(
                ModelProviderKey(provider_key="zhipu", owner="u1", api_key="sk-mine")
            )
            await db.commit()

            keys = await model_pool.load_personal_keys(db, "u1")
            assert keys == {"zhipu": "sk-mine"}

            await model_pool.refresh_snapshot(db)
            spec = model_pool.resolve_model("chat", owner="u1", personal_keys=keys)
            assert spec.api_key == "sk-mine"
            assert spec.cred_owner == "u1", "烧的是用户自己的 Key"

            # 没填个人 Key 的用户走全局
            spec2 = model_pool.resolve_model("chat", owner="u2", personal_keys={})
            assert spec2.api_key == "sk-global" and spec2.cred_owner == "system"
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 6. 熔断

def test_mark_failure_breaks_circuit_then_success_clears(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        async with Session() as db:
            db.add(_provider())
            only = _catalog()
            db.add(only)
            await db.flush()
            db.add(ModelBinding(feature="chat", primary_model_id=only.id, fallbacks=[]))
            await db.commit()
            await model_pool.refresh_snapshot(db)

            spec = model_pool.resolve_model("chat")
            assert spec is not None
            await model_pool.mark_failure(spec, "401 凭证无效")
            # 熔断后解析不到 → 回落环境变量通道
            assert model_pool.resolve_model("chat") is None

            await model_pool.mark_success(spec)
            assert model_pool.resolve_model("chat") is not None
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 7. 播种与迁移

def test_seed_from_env_and_migrate_legacy(tmp_path):
    async def _run():
        engine, Session = _mk(tmp_path, "seed.db")
        await _init(engine)
        async with Session() as db:
            n = await model_pool.seed_model_pool(db)
            await db.commit()
            assert n["seeded"] > 0, ".env 里有 DeepSeek/CF 凭证就该播种出东西"

            binds = (await db.execute(select(ModelBinding))).scalars().all()
            feats = {b.feature for b in binds}
            assert {"chat", "layout", "image", "embedding"} <= feats, feats

            # 再调一次必须幂等（不能覆盖管理员后来的改动）
            again = await model_pool.seed_model_pool(db)
            assert again["seeded"] == 0

            # 旧设置迁移：chat_provider=cloudflare → chat 槽指向那个 CF 模型
            from app.models.settings import AppSetting

            cf_model = (
                await db.execute(
                    select(ModelCatalog).where(ModelCatalog.capabilities.contains('"image"'))
                )
            ).scalars().first()
            db.add(AppSetting(owner="system", key="chat_provider", value="cloudflare"))
            db.add(
                AppSetting(
                    owner="system", key="cloudflare_chat_model", value=cf_model.model_id
                )
            )
            await db.commit()
            ok = await model_pool.migrate_legacy_chat_provider(db)
            await db.commit()
            assert ok is True
            chat_bind = (
                await db.execute(select(ModelBinding).where(ModelBinding.feature == "chat"))
            ).scalar_one()
            assert chat_bind.primary_model_id == cf_model.id

        model_pool.invalidate()

    asyncio.run(_run())
