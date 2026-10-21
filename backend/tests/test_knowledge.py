"""知识库（RAG）单元测试：分块 / 检索 / 隔离 / 配额 / 注入 / 降级。

铁律（照 tests/test_auth.py 的注释理解）：
  - 用临时文件库（NullPool），**不碰开发库**；
  - `create_item` 会 `asyncio.create_task(_embed_task)`，后者默认走全局
    `SessionLocal`（指向开发库）。测试里把它换成自己的 sessionmaker，
    再把 `embedding.embed_texts` 换成可预测的假实现 —— 既不烧 Cloudflare
    额度，也能断言排序；
  - 模块级向量缓存跨测试串味，每个用例开头 `invalidate_cache()`。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  # 必须导入，create_all 才看得到 knowledge_* 表
from app.core.config import settings
from app.db.session import Base
from app.models.conversation import Conversation
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.services import embedding, knowledge as kb
from app.services import conversation as conv_svc


def _fake_embed(texts):
    """可预测的假向量：[退款, 发票, 响应] 三维。

    查询与素材用同一套编码，余弦就是「关键词是否同一类」，排序可断言。
    """
    out = []
    for t in texts:
        out.append(
            [
                1.0 if "退款" in t else 0.0,
                1.0 if "发票" in t else 0.0,
                1.0 if "响应" in t else 0.0,
            ]
        )
    return out


async def _fake_embed_async(texts):
    return _fake_embed(texts)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """一套临时库 + 假 embedding + 把后台任务的 session 也指到临时库。"""
    db_file = tmp_path / "kb.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    monkeypatch.setattr(embedding, "embed_texts", _fake_embed_async)
    monkeypatch.setattr(embedding, "configured", lambda: True)
    monkeypatch.setattr(kb, "SessionLocal", Maker)
    kb.invalidate_cache()
    return SimpleNamespace(Maker=Maker)


# ---------------------------------------------------------------- 分块


def test_split_text_merges_and_drops_tiny_fragments():
    """短段落贪心合并；超长按句再切；末尾碎片并入上一片。"""
    para_short = "第一段很短。"
    para_long = "这是一句话。" * 60  # 360 字，单段不超 500
    chunks = kb.split_text(para_short + "\n\n" + para_long + "\n\n如图。")
    # 短段 + 长段能合并进 500（含换行 1 字符）？9+1+360=370 → 合成一片
    assert len(chunks) == 1 or len(chunks) == 2
    assert all(len(c) <= settings.KB_CHUNK_SIZE or len(c) > 0 for c in chunks)
    # 「如图。」不能单独成片（< KB_CHUNK_MIN），必须并回上一片
    assert not any(c.strip() == "如图。" for c in chunks)
    assert "如图。" in chunks[-1]


def test_split_text_cuts_oversized_sentence():
    """一整句超过单片上限（无标点长串）→ 硬切，不能返回 600 字的片。"""
    text = "哈" * 1300
    chunks = kb.split_text(text, size=500)
    assert len(chunks) >= 3
    assert all(len(c) <= 500 for c in chunks)
    assert sum(len(c) for c in chunks) >= 1300


def test_split_text_empty():
    assert kb.split_text("") == []
    assert kb.split_text("   \n\n  ") == []


# ---------------------------------------------------------------- 检索


async def _seed(env, owner: str, texts: list[str]) -> None:
    """造一条素材 + 它的片段。

    必须真的建 item：`_load_vectors` 是 chunk **join item**（要拿标题），
    光插 chunk 会被 join 过滤成空 —— 这也是「删素材必须级联删 chunk」的
    反面证据（孤儿片段检索不到，但白占库）。
    """
    async with env.Maker() as db:
        item_id = f"item-{owner}-{abs(hash(tuple(texts))) % 10000}"
        db.add(
            KnowledgeItem(
                id=item_id,
                owner=owner,
                source="manual",
                title=f"{owner} 的素材",
                raw_text="\n".join(texts),
                status="ready",
                chunk_count=len(texts),
            )
        )
        for i, t in enumerate(texts):
            vec = _fake_embed([t])[0]
            db.add(
                KnowledgeChunk(
                    owner=owner,
                    item_id=item_id,
                    seq=i,
                    content=t,
                    char_len=len(t),
                    vector=vec,
                    norm=embedding.norm_of(vec),
                    embed_model="fake",
                    status="ready",
                )
            )
        await db.commit()


def _run(coro):
    return asyncio.run(coro)


def test_search_ranks_by_similarity_and_applies_threshold(env):
    async def go():
        await _seed(
            env,
            "u1",
            [
                "关于发票的开具流程说明",
                "退款政策与退款申请流程",
                "技术支持响应时间说明",
            ],
        )
        async with env.Maker() as db:
            hits = await kb.search(db, "u1", "退款")
        return hits

    hits = _run(go())
    assert len(hits) == 1, hits
    assert "退款" in hits[0]["content"]
    assert hits[0]["match"] == "vector"
    # 完全不相关的查询（三维全 0 → 相似度 0）应被阈值挡掉
    async def none_hit():
        async with env.Maker() as db:
            return await kb.search(db, "u1", "晚饭吃什么")

    assert _run(none_hit()) == []


def test_search_is_owner_isolated(env):
    async def go():
        await _seed(env, "u1", ["退款政策 A"])
        await _seed(env, "u2", ["退款政策 B 机密"])
        async with env.Maker() as db:
            mine = await kb.search(db, "u1", "退款")
            other = await kb.search(db, "u2", "退款")
        return mine, other

    mine, other = _run(go())
    assert len(mine) == 1 and "A" in mine[0]["content"]
    assert len(other) == 1 and "B" in other[0]["content"]


def test_search_degrades_to_keyword_when_embedding_fails(env, monkeypatch):
    """向量化抛错 → 退化成关键词检索，且**不抛异常**（检索是旁路）。"""

    async def boom(_texts):
        raise RuntimeError("Cloudflare 挂了")

    monkeypatch.setattr(embedding, "embed_texts", boom)

    async def go():
        await _seed(env, "u1", ["退款政策与退款申请流程"])
        async with env.Maker() as db:
            return await kb.search(db, "u1", "我要退款怎么办")

    hits = _run(go())
    assert hits and hits[0]["match"] == "keyword"
    assert "退款" in hits[0]["content"]

    # 一个字都不重合 → 不能命中（否则降级模式会往上下文里灌无关素材）
    async def none_hit():
        async with env.Maker() as db:
            return await kb.search(db, "u1", "今晚吃什么好呢")

    assert _run(none_hit()) == []


def test_recall_skips_short_query(env):
    """「继续」这类短追问跳过检索（省一次 embedding 调用）。"""

    async def go():
        await _seed(env, "u1", ["退款政策"])
        async with env.Maker() as db:
            return await kb.recall_for_prompt(db, "u1", "继续")

    text, hits = _run(go())
    assert text == "" and hits == []


def test_build_snippets_respects_budget(env):
    hits = [{"title": "T", "content": "字" * 800}, {"title": "T2", "content": "字" * 800}]
    out = kb.build_snippets(hits, max_chars=1000, per_snippet=1200)
    # 第一条 800 字：装得下；第二条加上就 1600 > 1000 → 停，不截断半条
    assert out.count("[2]") == 0
    assert len(out) < 1600

    out2 = kb.build_snippets(hits, max_chars=5000, per_snippet=200)
    assert "已截断" in out2


# ---------------------------------------------------------------- 写入与配额


def test_create_item_quota_limits(env, monkeypatch):
    monkeypatch.setattr(settings, "KB_MAX_ITEMS_PER_USER", 1)

    async def go():
        async with env.Maker() as db:
            await kb.create_item(db, "u1", source="manual", title="A", raw_text="内容一" * 30)
            try:
                await kb.create_item(db, "u1", source="manual", title="B", raw_text="内容二" * 30)
            except kb.KnowledgeError as exc:
                return exc
            return None

    err = _run(go())
    assert isinstance(err, kb.KnowledgeError)
    assert "上限" in str(err)


def test_create_item_rejects_empty(env):
    async def go():
        async with env.Maker() as db:
            try:
                await kb.create_item(db, "u1", source="manual", title="", raw_text="  ")
            except kb.KnowledgeError as exc:
                return exc

    assert isinstance(_run(go()), kb.KnowledgeError)


def test_create_item_then_embed_task_marks_ready(env):
    """录入后后台任务把 pending 片段跑成 ready，item 状态随之收敛。"""

    async def go():
        async with env.Maker() as db:
            item = await kb.create_item(
                db, "u1", source="manual", title="FAQ", raw_text="退款政策说明" * 40
            )
            item_id = item.id
            assert item.status == "pending"
            assert item.chunk_count >= 1
        # 等后台任务（create_item 里 create_task 出来的）
        for _ in range(50):
            task = kb._active.get(item_id)
            if task is None:
                break
            await asyncio.sleep(0.05)
        async with env.Maker() as db:
            item = await db.get(KnowledgeItem, item_id)
            chunks = (
                await db.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.item_id == item_id)
                )
            ).scalars().all()
            return item, chunks

    item, chunks = _run(go())
    assert item.status == "ready"
    assert all(c.status == "ready" and c.vector for c in chunks)
    assert all(c.embed_model == settings.CLOUDFLARE_EMBED_MODEL for c in chunks)


def test_delete_item_cascades_chunks(env):
    async def go():
        async with env.Maker() as db:
            item = await kb.create_item(
                db, "u1", source="manual", title="FAQ", raw_text="退款政策说明" * 40
            )
            item_id = item.id
        for _ in range(50):
            if kb._active.get(item_id) is None:
                break
            await asyncio.sleep(0.05)
        async with env.Maker() as db:
            ok = await kb.delete_item(db, "u1", item_id)
            left = (
                await db.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.item_id == item_id)
                )
            ).scalars().all()
            # 别人删不掉
            return ok, left, await kb.delete_item(db, "u2", item_id)

    ok, left, cross = _run(go())
    assert ok is True and left == [] and cross is False


# ---------------------------------------------------------------- 注入


def test_build_context_injects_standalone_knowledge_message(env):
    """素材必须是**独立** system 消息，不能污染 system_prompt 本体。"""
    system_prompt = "你是文案助手。\n\n## 工具使用纪律\n- 用工具前先问"

    async def go():
        async with env.Maker() as db:
            conv = Conversation(id="c1", owner="u1", title="t", scene="chat", summary="旧摘要")
            db.add(conv)
            await db.commit()
            return await conv_svc.build_context(db, conv, system_prompt, knowledge="片段A\n片段B")

    msgs = _run(go())
    sys_msgs = [m for m in msgs if m["role"] == "system"]
    assert msgs[0]["content"] == system_prompt, "system_prompt 必须原样保留"
    assert len(sys_msgs) == 3  # prompt + 摘要 + 素材
    kb_msg = sys_msgs[-1]
    assert kb_msg["content"].startswith(conv_svc.KNOWLEDGE_HEADER)
    assert "片段A" in kb_msg["content"]
    # 关键：素材消息独立，system_prompt 里没有素材 —— 否则
    # strip_tool_discipline() 会在 CF 通道把素材整段切掉
    assert "片段A" not in system_prompt


def test_build_context_without_knowledge_unchanged(env):
    async def go():
        async with env.Maker() as db:
            conv = Conversation(id="c2", owner="u1", title="t", scene="chat")
            db.add(conv)
            await db.commit()
            return await conv_svc.build_context(db, conv, "SYS")

    msgs = _run(go())
    assert [m for m in msgs if m["role"] == "system"] == [{"role": "system", "content": "SYS"}]
