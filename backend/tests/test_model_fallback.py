"""流式自动降级的行为契约。

这里测的是三条「不写进代码就一定出事」的语义：
  1. 开流就失败 → 切下一个候选，用户只看到一次成功输出（不出现两段拼接）；
  2. 已经吐过正文再失败 → **绝不换模型**（否则同一条回答里会出现两段风格
     迥异的正文，而且极难复现），但错误必须告诉用户；
  3. 额度用到 80% 只预警一次，用到 100% 才升级成「已用尽」—— 每次对话都弹
     等于没弹。
"""
from __future__ import annotations

import asyncio
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db.session import Base
from app.models.model_pool import ModelBinding, ModelCatalog, ModelProvider
from app.services import llm as llm_mod
from app.services import model_pool


async def _mk(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fb.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        db.add(
            ModelProvider(
                owner="system", key="p", name="测试服务商", kind="openai",
                base_url="http://127.0.0.1:1/v1", api_key="sk-x", enabled=True,
            )
        )
        rows = {}
        for mid in ("m-bad", "m-half", "m-ok"):
            c = ModelCatalog(
                provider_key="p", model_id=mid, label=mid, capabilities=["chat"],
                protocol="openai_chat", enabled=True,
            )
            db.add(c)
            rows[mid] = c
        await db.flush()
        db.add(
            ModelBinding(
                feature="chat",
                primary_model_id=rows["m-bad"].id,
                fallbacks=[rows["m-ok"].id],
            )
        )
        db.add(
            ModelBinding(
                feature="layout",
                primary_model_id=rows["m-half"].id,
                fallbacks=[rows["m-ok"].id],
            )
        )
        await db.commit()
        await model_pool.refresh_snapshot(db)
    return engine, Session


async def _fake_run_stream(
    self, client, kwargs, task, model, usage_sink,
    spec=None, emit_error=True, state=None,
):
    """假流：按模型名决定「开流即失败 / 吐一半再失败 / 成功」。"""
    try:
        if model == "m-bad":
            raise RuntimeError("开流即失败（凭证无效）")
        if model == "m-half":
            if state is not None:
                state["produced"] = True
            yield {"type": "delta", "content": "半句正文"}
            raise RuntimeError("输出中途断开")
        yield {"type": "delta", "content": "完整正文"}
        yield {"type": "finish", "finish_reason": "stop"}
    except RuntimeError as exc:
        if state is not None:
            state["failed"] = True
        if emit_error:
            yield {"type": "error", "message": f"模型调用失败：{exc}"}


async def _collect(agen):
    return [e async for e in agen]


def test_switch_when_stream_fails_before_any_output(tmp_path):
    async def _run():
        engine, Session = await _mk(tmp_path)
        llm = llm_mod.LLMClient()
        llm._run_stream = _fake_run_stream.__get__(llm, llm_mod.LLMClient)
        events = await _collect(llm.stream_call([{"role": "user", "content": "hi"}], feature="chat"))
        kinds = [e["type"] for e in events]
        # 开流即失败 → 换到 m-ok，且**不能**把 error 抛给用户（那会让前端以为结束）
        assert "error" not in kinds, events
        assert "model_switched" in kinds, events
        assert [e.get("content") for e in events if e["type"] == "delta"] == ["完整正文"]
        model_pool.invalidate()
        await engine.dispose()

    asyncio.run(_run())


def test_no_switch_after_output_started(tmp_path):
    async def _run():
        engine, Session = await _mk(tmp_path)
        llm = llm_mod.LLMClient()
        llm._run_stream = _fake_run_stream.__get__(llm, llm_mod.LLMClient)
        events = await _collect(llm.stream_call([{"role": "user", "content": "hi"}], feature="layout"))
        kinds = [e["type"] for e in events]
        # 已经吐过正文 → 不再换模型（否则回答被拼成两段），但错误要告诉用户
        assert "model_switched" not in kinds, events
        assert "error" in kinds, events
        assert [e.get("content") for e in events if e["type"] == "delta"] == ["半句正文"]
        model_pool.invalidate()
        await engine.dispose()

    asyncio.run(_run())


def test_all_candidates_fail_yields_error(tmp_path):
    async def _run():
        engine, Session = await _mk(tmp_path)
        async with Session() as db:
            # 把 m-ok 也换成坏模型：整条链都不可用
            ok = (
                await db.execute(select(ModelCatalog).where(ModelCatalog.model_id == "m-ok"))
            ).scalar_one()
            ok.model_id = "m-bad2"
            await db.commit()
            await model_pool.refresh_snapshot(db)
        llm = llm_mod.LLMClient()
        # m-bad2 也走「开流即失败」分支
        orig = _fake_run_stream

        async def fake2(self, client, kwargs, task, model, usage_sink,
                        spec=None, emit_error=True, state=None):
            try:
                if model.startswith("m-bad"):
                    raise RuntimeError("全挂了")
                yield {"type": "finish", "finish_reason": "stop"}
            except RuntimeError as exc:
                if state is not None:
                    state["failed"] = True
                if emit_error:
                    yield {"type": "error", "message": f"模型调用失败：{exc}"}

        llm._run_stream = fake2.__get__(llm, llm_mod.LLMClient)
        events = await _collect(llm.stream_call([{"role": "user", "content": "hi"}], feature="chat"))
        kinds = [e["type"] for e in events]
        assert kinds.count("error") == 1, events  # 最后一个候选才发 error
        assert "model_switched" in kinds
        model_pool.invalidate()
        await engine.dispose()

    asyncio.run(_run())


def test_quota_warning_fires_once_per_threshold(tmp_path):
    async def _run():
        engine, Session = await _mk(tmp_path)
        async with Session() as db:
            primary = (
                await db.execute(select(ModelCatalog).where(ModelCatalog.model_id == "m-bad"))
            ).scalar_one()
            primary.daily_calls_limit = 10
            await db.commit()
            await model_pool.refresh_snapshot(db)

        spec = model_pool.resolve_model("chat")
        # 造一条 90% 的用量（9/10）
        snap = model_pool.snapshot()
        snap.quota[(spec.model, spec.cred_owner, "day", model_pool._period_key("day"))] = {
            "calls": 9, "tokens": 0, "images": 0,
        }
        warns = model_pool.take_warnings(spec, "chat")
        assert len(warns) == 1 and warns[0]["level"] == "warn", warns
        # 同一阈值不重复发
        assert model_pool.take_warnings(spec, "chat") == []
        model_pool._warned.clear()
        model_pool.invalidate()
        await engine.dispose()

    asyncio.run(_run())
