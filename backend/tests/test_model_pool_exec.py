"""模型池执行器（生图 / 向量化）与 embedding 三道防线的单元测试。

--------------------------------------------------------------------------
为什么这些必须测
--------------------------------------------------------------------------
1. **换 embedding 模型 = 旧向量全部作废。** 不同模型产出的向量不在同一个
   空间里，混着比余弦得到的是噪声，而且**不报错**——表现为知识库突然什么
   都搜不到，是那种能让人查一下午的故障。所以 `_load_vectors` 必须按
   `embed_model` 严格过滤，这条必须有测试锁死。

2. **各家响应结构千奇百怪。** CF 的文生图返回单数字段 `image`（不是
   `images`），向量可能嵌 `data.data`，单条输入返回一维数组。这些坑在
   `imagegen.py` / `embedding.py` 里各踩过一遍，收口到 `model_exec` 后
   要把「仍然认得」固化下来。

3. **保存时拦住，好过线上出怪事。** embedding 槽不许有备选链、备选链能力
   必须与主模型一致、换 embedding 模型要先确认失效条数——三道校验各一测。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  建表需要
from app.db.session import Base
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.models.model_pool import ModelBinding, ModelCatalog, ModelProvider
from app.services import embedding, knowledge as kb
from app.services import model_exec, model_pool


def _mk(tmp_path, name="exec.db"):
    # NullPool：记账/熔断走的是 model_pool 自己的 SessionLocal（测试里会换成
    # 这个 maker），与主 session 并发写同一个 SQLite 文件，排队池会打架。
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}", poolclass=NullPool
    )
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _init(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _spec(**kw):
    base = dict(
        model="@cf/black-forest-labs/flux-1-schnell",
        provider_key="cloudflare",
        protocol="cf_ai_run_image",
        base_url="https://api.cloudflare.com/client/v4/accounts/acct1/ai/v1",
        api_key="tok",
        catalog_id="c1",
        label="CF 文生图",
    )
    base.update(kw)
    return model_pool.ModelSpec(**base)


# ---------------------------------------------------------------- 1. URL 拼装

def test_cf_endpoint_strips_ai_v1():
    """CF 的 base_url 记的是 OpenAI 兼容入口 /ai/v1，REST 端点是同级 /ai/run。"""
    spec = _spec()
    assert model_exec._endpoint(spec, "image") == (
        "https://api.cloudflare.com/client/v4/accounts/acct1/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )
    openai_spec = _spec(
        protocol="openai_images",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-image",
    )
    assert model_exec._endpoint(openai_spec, "image") == (
        "https://open.bigmodel.cn/api/paas/v4/images/generations"
    )
    embed_spec = _spec(
        protocol="openai_embeddings",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-embedding",
    )
    assert model_exec._endpoint(embed_spec, "embed").endswith("/embeddings")


# ---------------------------------------------------------------- 2. 响应解析容错

def test_extract_images_handles_cf_single_field():
    """CF flux 返回的是单数 `image`，只认 `images` 会把好图当异常扔掉。"""
    import base64

    b64 = base64.b64encode(b"\x89PNG-fake").decode()
    out = model_exec._extract_images({"result": {"image": b64}})
    assert out == [b"\x89PNG-fake"]
    # 数组形态也要认
    out2 = model_exec._extract_images({"result": {"images": [b64]}})
    assert out2 == [b"\x89PNG-fake"]
    # OpenAI 形态：b64_json
    out3 = model_exec._extract_images({"data": [{"b64_json": b64}]})
    assert out3 == [b"\x89PNG-fake"]
    # 空响应不能崩，返回空列表（由调用方判为异常）
    assert model_exec._extract_images({"result": {}}) == []


def test_extract_vectors_handles_both_shapes():
    # OpenAI: data[].embedding（可能乱序，按 index 排回来）
    vecs, tok = model_exec._extract_vectors(
        {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 12},
        }
    )
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]
    assert tok == 12

    # CF: result.data 二维
    vecs2, tok2 = model_exec._extract_vectors({"result": {"data": [[0.5, 0.5]]}})
    assert vecs2 == [[0.5, 0.5]] and tok2 == 0

    # CF: result.data 一维（单条输入）
    vecs3, _ = model_exec._extract_vectors({"result": {"data": [0.5, 0.5]}})
    assert vecs3 == [[0.5, 0.5]]

    # 嵌套 data.data（有的网关多包一层）
    vecs4, _ = model_exec._extract_vectors({"result": {"data": {"data": [[0.1]]}}})
    assert vecs4 == [[0.1]]

    with pytest.raises(model_exec.ModelExecError):
        model_exec._extract_vectors({"result": {}})


# ---------------------------------------------------------------- 3. 执行 + 记账 + 熔断

def test_run_image_accounts_quota_and_clears_breaker(tmp_path, monkeypatch):
    """成功调用要记账（额度）、失败要熔断（下次别再撞它）。"""
    import base64

    b64 = base64.b64encode(b"PNG-bytes").decode()

    async def _fake_post(spec, payload, kind):
        assert kind == "image"
        assert payload == {"prompt": "一只猫"}
        return {"result": {"image": b64}}

    monkeypatch.setattr(model_exec, "_post", _fake_post)

    async def _run():
        engine, Session = _mk(tmp_path)
        await _init(engine)
        # 记账走 model_pool 自己的 SessionLocal —— 指到临时库，否则会写进开发库
        monkeypatch.setattr(model_pool, "SessionLocal", Session)
        async with Session() as db:
            db.add(ModelProvider(
                owner="system", key="cloudflare", name="CF", kind="cloudflare",
                base_url="https://api.cloudflare.com/client/v4/accounts/a/ai/v1",
                api_key="tok", enabled=True, quota_reset_tz="utc",
            ))
            db.add(ModelCatalog(
                id="c1", provider_key="cloudflare", model_id="@cf/flux",
                label="flux", protocol="cf_ai_run_image", capabilities=["image"],
                billing="free", quota_scope="provider", enabled=True,
            ))
            await db.commit()
            await model_pool.refresh_snapshot(db)

            spec = _spec(model="@cf/flux", catalog_id="c1")
            got = await model_exec.run_image(spec, "一只猫", n=1)
            assert got == [b"PNG-bytes"]

            # 记账：日/月两行各记一次，张数各 +1
            rows = (
                await db.execute(select(model_pool.ModelQuotaUsage))
            ).scalars().all()
            assert rows, "调用没有记进额度表"
            assert sum(r.calls for r in rows) == 2  # day + month
            assert sum(r.images for r in rows) == 2
        model_pool.invalidate()

    asyncio.run(_run())


def test_run_embed_failure_breaks_the_model(tmp_path, monkeypatch):
    async def _boom(spec, payload, kind):
        raise model_exec.ModelExecError("上游 401")

    monkeypatch.setattr(model_exec, "_post", _boom)

    async def _run():
        engine, Session = _mk(tmp_path, "embed.db")
        await _init(engine)
        # 熔断写库走 model_pool 自己的 SessionLocal —— 指到临时库
        monkeypatch.setattr(model_pool, "SessionLocal", Session)
        async with Session() as db:
            db.add(ModelProvider(
                owner="system", key="cloudflare", name="CF", kind="cloudflare",
                base_url="https://api.cloudflare.com/client/v4/accounts/a/ai/v1",
                api_key="tok", enabled=True, quota_reset_tz="utc",
            ))
            db.add(ModelCatalog(
                id="c1", provider_key="cloudflare", model_id="@cf/bge-m3",
                label="bge", protocol="cf_ai_run_embed", capabilities=["embedding"],
                dimensions=1024, billing="free", quota_scope="provider", enabled=True,
            ))
            await db.commit()
            await model_pool.refresh_snapshot(db)

            spec = _spec(model="@cf/bge-m3", catalog_id="c1", protocol="cf_ai_run_embed")
            with pytest.raises(model_exec.ModelExecError):
                await model_exec.run_embed(spec, ["你好"])

            # 熔断：该模型 5 分钟内不再被选中
            row = await db.get(ModelCatalog, "c1")
            assert row.disabled_until > int(time.time())
            assert "401" in (row.last_error or "")
            # 且内存快照同步了（否则本进程内还会继续撞它）
            assert model_pool.snapshot().catalog["c1"]["disabled_until"] > 0
        model_pool.invalidate()

    asyncio.run(_run())


# ---------------------------------------------------------------- 4. embedding 三道防线

def _fake_vec(text: str) -> list[float]:
    return [1.0 if "退款" in text else 0.0, 1.0 if "发票" in text else 0.0]


async def _seed_chunks(db, owner: str, texts: list[str], model: str) -> str:
    item_id = f"it-{owner}-{abs(hash((tuple(texts), model))) % 9999}"
    db.add(KnowledgeItem(
        id=item_id, owner=owner, source="manual", title="FAQ",
        raw_text="\n".join(texts), status="ready", chunk_count=len(texts),
    ))
    for i, t in enumerate(texts):
        v = _fake_vec(t)
        db.add(KnowledgeChunk(
            owner=owner, item_id=item_id, seq=i, content=t, char_len=len(t),
            vector=v, norm=embedding.norm_of(v), embed_model=model, status="ready",
        ))
    await db.commit()
    return item_id


def test_load_vectors_isolates_by_embed_model(tmp_path):
    """**最关键的一条**：换模型后旧向量不得参与向量检索。

    不对它们做过滤，余弦相似度会在两个不相干的空间里比 —— 结果是噪声，
    而且不报错，表现为「知识库突然什么都搜不到」。
    """
    async def _run():
        engine, Session = _mk(tmp_path, "kb.db")
        await _init(engine)
        kb.invalidate_cache()
        async with Session() as db:
            await _seed_chunks(db, "u1", ["退款政策与退款申请流程"], "old-model")
            await _seed_chunks(db, "u1", ["发票开具流程说明"], "new-model")

            old = await kb._load_vectors(db, "u1", "old-model")
            new = await kb._load_vectors(db, "u1", "new-model")
            assert [r["content"] for r in old] == ["退款政策与退款申请流程"]
            assert [r["content"] for r in new] == ["发票开具流程说明"]

            # 全量文本（关键词检索用）不受模型过滤影响 —— 重建期间仍可检索
            all_rows = await kb._load_texts(db, "u1")
            assert len(all_rows) == 2
        kb.invalidate_cache()

    asyncio.run(_run())


def test_search_falls_back_to_keyword_after_model_switch(tmp_path, monkeypatch):
    """换模型后：向量检索拿不到东西，但关键词检索仍然能召回（不静默变空）。"""
    async def _fake_ready(owner=None, db=None):
        return True

    async def _fake_model(owner=None, db=None):
        return "new-model"

    async def _fake_embed_one(text, owner=None, db=None):
        return _fake_vec(text)

    monkeypatch.setattr(embedding, "ready", _fake_ready)
    monkeypatch.setattr(embedding, "current_model", _fake_model)
    monkeypatch.setattr(embedding, "embed_one", _fake_embed_one)

    async def _run():
        engine, Session = _mk(tmp_path, "kb2.db")
        await _init(engine)
        kb.invalidate_cache()
        async with Session() as db:
            # 素材是用 old-model 算的向量，当前模型是 new-model
            await _seed_chunks(db, "u1", ["退款政策与退款申请流程"], "old-model")
            hits = await kb.search(db, "u1", "退款")
        assert hits, "换模型后关键词检索应当仍然可用"
        assert hits[0]["match"] == "keyword"
        assert "退款" in hits[0]["content"]
        kb.invalidate_cache()

    asyncio.run(_run())


def test_reembed_stale_queues_old_vectors(tmp_path, monkeypatch):
    """重建入口：把过期片段置回 pending，并让父素材状态跟着回退。"""
    async def _fake_ready(owner=None, db=None):
        return True

    async def _fake_model(owner=None, db=None):
        return "new-model"

    monkeypatch.setattr(embedding, "ready", _fake_ready)
    monkeypatch.setattr(embedding, "current_model", _fake_model)

    async def _run():
        engine, Session = _mk(tmp_path, "kb3.db")
        await _init(engine)
        kb.invalidate_cache()
        monkeypatch.setattr(kb, "SessionLocal", Session)
        async with Session() as db:
            await _seed_chunks(db, "u1", ["退款政策说明"], "old-model")
            stale = await kb.count_stale(db, "u1", "new-model")
            assert stale == 1

            res = await kb.reembed_stale(db, "u1")
            assert res["queued"] == 1 and res["items"] == 1

            chunks = (
                await db.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.owner == "u1")
                )
            ).scalars().all()
            assert all(c.status == "pending" for c in chunks)
            # 重建后再查：没有过期片段了
            assert await kb.count_stale(db, "u1", "new-model") == 0
        kb.invalidate_cache()

    asyncio.run(_run())


# ---------------------------------------------------------------- 5. 绑定保存校验

def _save_binding_env(monkeypatch, tmp_path):
    """造一个够用的池：两个 chat 模型 + 两个不同维度的 embedding 模型。"""
    engine, Session = _mk(tmp_path, "bind.db")

    async def _setup():
        await _init(engine)
        async with Session() as db:
            db.add(ModelProvider(
                owner="system", key="zhipu", name="智谱", kind="openai",
                base_url="https://open.bigmodel.cn/api/paas/v4",
                api_key="sk", enabled=True,
            ))
            db.add(ModelCatalog(
                id="m-chat-a", provider_key="zhipu", model_id="glm-4-flash",
                label="A", capabilities=["chat"], protocol="openai_chat",
                supports_tools=True, enabled=True, sort=10,
            ))
            db.add(ModelCatalog(
                id="m-chat-b", provider_key="zhipu", model_id="glm-4-flash-b",
                label="B", capabilities=["chat"], protocol="openai_chat",
                supports_tools=False, enabled=True, sort=11,
            ))
            db.add(ModelCatalog(
                id="m-emb-1", provider_key="zhipu", model_id="embedding-3",
                label="向量 1024", capabilities=["embedding"],
                protocol="openai_embeddings", dimensions=1024, enabled=True, sort=20,
            ))
            db.add(ModelCatalog(
                id="m-emb-2", provider_key="zhipu", model_id="embedding-2",
                label="向量 768", capabilities=["embedding"],
                protocol="openai_embeddings", dimensions=768, enabled=True, sort=21,
            ))
            await db.commit()
            await model_pool.refresh_snapshot(db)

    return engine, Session, _setup


def test_binding_rejects_capability_mismatch(monkeypatch, tmp_path):
    """备选模型工具能力不一致 → 保存时就拒掉。

    否则降级发生的那一刻，Agent 的工具循环会在中途静默失效（不报错，
    只是再也叫不动工具），比直接失败难查得多。
    """
    from fastapi import HTTPException

    from app.routers import model_pool as mp_router

    engine, Session, _setup = _save_binding_env(monkeypatch, tmp_path)

    async def _run():
        await _setup()
        async with Session() as db:
            with pytest.raises(HTTPException) as ei:
                await mp_router.save_binding(
                    "chat",
                    {"primary": "m-chat-a", "fallbacks": ["m-chat-b"]},
                    db=db,
                    owner="u1",
                    _admin=None,
                )
            assert "工具调用能力" in str(ei.value.detail)
        model_pool.invalidate()

    asyncio.run(_run())


def test_binding_rejects_fallback_on_embedding(monkeypatch, tmp_path):
    """embedding 槽不许配备选：换模型是「旧向量全废」，不能靠降级悄悄换。"""
    from fastapi import HTTPException

    from app.routers import model_pool as mp_router

    engine, Session, _setup = _save_binding_env(monkeypatch, tmp_path)

    async def _run():
        await _setup()
        async with Session() as db:
            with pytest.raises(HTTPException) as ei:
                await mp_router.save_binding(
                    "embedding",
                    {"primary": "m-emb-1", "fallbacks": ["m-emb-2"]},
                    db=db,
                    owner="u1",
                    _admin=None,
                )
            assert "不支持备选模型" in str(ei.value.detail)
        model_pool.invalidate()

    asyncio.run(_run())


def test_switching_embed_model_returns_409_with_stale_count(monkeypatch, tmp_path):
    """换 embedding 模型 → 409 EMBED_DIM_MISMATCH，带失效条数。"""
    from fastapi import HTTPException

    from app.routers import model_pool as mp_router

    engine, Session, _setup = _save_binding_env(monkeypatch, tmp_path)

    async def _run():
        await _setup()
        async with Session() as db:
            # 先绑 1024 维那个
            await mp_router.save_binding(
                "embedding", {"primary": "m-emb-1"}, db=db, owner="u1", _admin=None
            )
            # 造 3 条「用 1024 模型算出来」的向量
            await _seed_chunks(db, "u1", ["退款政策", "发票流程", "响应时间"], "embedding-3")

            # 换 768 维 → 409，且要报出会失效几条
            with pytest.raises(HTTPException) as ei:
                await mp_router.save_binding(
                    "embedding", {"primary": "m-emb-2"}, db=db, owner="u1", _admin=None
                )
            assert ei.value.status_code == 409
            detail = ei.value.detail
            assert detail["code"] == "EMBED_DIM_MISMATCH"
            assert detail["stale_chunks"] == 3
            assert detail["from_model"] == "embedding-3"
            assert detail["to_model"] == "embedding-2"

            # 带 force=true 才保存
            res = await mp_router.save_binding(
                "embedding", {"primary": "m-emb-2", "force": True},
                db=db, owner="u1", _admin=None,
            )
            assert res["primary"] == "m-emb-2"
        model_pool.invalidate()
        kb.invalidate_cache()

    asyncio.run(_run())
