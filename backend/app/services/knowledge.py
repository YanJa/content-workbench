"""知识库服务：分块 / 向量化任务 / 检索 / 注入片段。

把「一条素材」变成「可语义检索的一堆片段」，再在写稿时按语义把最相关的
几片塞回模型上下文。整个模块的设计前提是：**它是写稿的旁路**——
向量化失败、CF 额度打满、网络抖动，都只能让召回降级，不能让对话报错。

三条纪律：
  1. 写链路（录入/上传）落库后立刻返回，向量化丢后台任务异步跑；
  2. 检索全链路 try/except，任何异常都退化成关键词检索（再失败就空结果）；
  3. 异步任务里绝不复用请求级 db session，一律 `async with SessionLocal()`
     （照 `services/render.py` 的惯例，否则 session 随请求关闭而失效）。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.services import embedding

# ---------------------------------------------------------------- 分块

# 句子级切分点。中文没有空格，靠标点断句；换行也算天然边界。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")
# 连续空白（含空行）——段落边界
_BLANK_RE = re.compile(r"\n\s*\n+")


def split_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """把长文本切成语义尽量完整的片段。

    策略（中文按**字符**计，不按 token——中文 1 字≈1 token，按字符算最直观）：
      1. 按空行断成段落；
      2. 贪心合并相邻段落，直到再加一段就超过 size；
      3. 单段本身就超长 → 按 `。！？；` 断句后再贪心合并；
      4. 仍超长（一整句就几百字，如无标点长串）→ 硬切成 size 长；
      5. 末尾不足 KB_CHUNK_MIN 的碎片并入上一片（「如图。」这种碎片检索没价值）。

    overlap：合并完成后，每片开头带上前一片末尾 overlap 个字符，缓解
    「一句话正好被切在两片边界」导致两边语义都不完整。
    """
    size = size or settings.KB_CHUNK_SIZE
    overlap = overlap or settings.KB_CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []

    blocks: list[str] = []
    for para in _BLANK_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            blocks.append(para)
        else:
            blocks.extend(_split_long(para, size))

    chunks = _greedy_merge(blocks, size)
    if len(chunks) > 1:
        # 短碎片并回上一片：只在有多个片段时才合并，避免单条素材被整片丢弃
        min_len = settings.KB_CHUNK_MIN
        merged: list[str] = []
        for c in chunks:
            if merged and len(c) < min_len:
                merged[-1] = merged[-1] + "\n" + c
            else:
                merged.append(c)
        chunks = merged

    if overlap > 0 and len(chunks) > 1:
        chunks = _apply_overlap(chunks, overlap, size)

    return chunks[: settings.KB_MAX_CHUNKS_PER_ITEM]


def _split_long(text: str, size: int) -> list[str]:
    """单段超长：先断句再贪心，仍不行就硬切。"""
    parts = [p for p in _SENT_SPLIT_RE.split(text) if p]
    if len(parts) == 1:
        parts = [text]
    out = _greedy_merge(parts, size)
    # 单句仍超长（无标点长串）：硬切，别让它撑爆 embedding 输入
    final: list[str] = []
    for p in out:
        while len(p) > size:
            final.append(p[:size])
            p = p[size:]
        if p:
            final.append(p)
    return final


def _greedy_merge(parts: list[str], size: int) -> list[str]:
    """贪心合并：能塞就塞，塞不下就开新片。"""
    out: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not cur:
            cur = p
        elif len(cur) + len(p) + 1 <= size:
            cur = cur + "\n" + p
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def _apply_overlap(chunks: list[str], overlap: int, size: int) -> list[str]:
    """给每片（除第一片）开头补上前一片末尾 overlap 字符。"""
    out = [chunks[0]]
    for i in range(1, len(chunks)):
        tail = chunks[i - 1][-overlap:]
        cand = tail + chunks[i]
        # 补完超过单片上限就只补一半，别为了重叠把片撑爆
        out.append(cand if len(cand) <= size else chunks[i])
    return out


# ---------------------------------------------------------------- 写入

async def _count_chunks(db: AsyncSession, owner: str) -> int:
    r = await db.execute(
        select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.owner == owner)
    )
    return int(r.scalar() or 0)


async def _count_items(db: AsyncSession, owner: str) -> int:
    r = await db.execute(
        select(func.count()).select_from(KnowledgeItem).where(KnowledgeItem.owner == owner)
    )
    return int(r.scalar() or 0)


class KnowledgeError(RuntimeError):
    """知识库写入被拒绝（超配额 / 内容为空 / 来源不存在）。

    与 embedding 失败区分开：这类是用户输入问题，要原样报给用户。
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def create_item(
    db: AsyncSession,
    owner: str,
    *,
    source: str,
    title: str,
    raw_text: str,
    origin_ref: str = "",
    file_path: str | None = None,
    meta: dict | None = None,
) -> KnowledgeItem:
    """落一条素材，切片后交给后台任务向量化。

    返回时 chunk 还是 pending（前端轮询 /status 即可显示「处理中」）——
    向量化要调外部 API，塞进请求里会让录入按钮转好几秒。
    """
    text = (raw_text or "").strip()
    if not text:
        raise KnowledgeError("内容为空，无法存入知识库")
    if len(text) > 500_000:
        raise KnowledgeError("内容过长（上限 50 万字），请先拆分后再存")

    if await _count_items(db, owner) >= settings.KB_MAX_ITEMS_PER_USER:
        raise KnowledgeError(
            f"知识库素材已达上限（{settings.KB_MAX_ITEMS_PER_USER} 条），请先删除一些"
        )
    chunks = split_text(text)
    if not chunks:
        raise KnowledgeError("没能从内容里切出可用片段")
    if await _count_chunks(db, owner) + len(chunks) > settings.KB_MAX_CHUNKS_PER_USER:
        raise KnowledgeError(
            f"知识库片段已达上限（{settings.KB_MAX_CHUNKS_PER_USER} 片），请先删除一些素材"
        )

    now = int(time.time())
    item = KnowledgeItem(
        owner=owner,
        source=source,
        title=(title or "").strip()[:300] or _auto_title(text),
        origin_ref=origin_ref or "",
        raw_text=text,
        file_path=file_path,
        status="pending",
        chunk_count=0,
        meta=meta or {},
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    await db.flush()

    for i, c in enumerate(chunks):
        db.add(
            KnowledgeChunk(
                owner=owner,
                item_id=item.id,
                seq=i,
                content=c,
                char_len=len(c),
                status="pending",
            )
        )
    item.chunk_count = len(chunks)
    await db.commit()
    await db.refresh(item)

    invalidate_cache(owner)
    task = asyncio.create_task(_embed_task(item.id))
    _active[item.id] = task
    task.add_done_callback(lambda _t, _id=item.id: _active.pop(_id, None))
    return item


# 视为「活跃」的状态：这些不会被去重/来源映射忽略。
# failed 的不算 —— 失败的素材后续应让用户重试，而不是把同一条成稿又存一份。
_ACTIVE_STATUSES = ("pending", "processing", "ready", "partial")


async def get_active_by_origin(
    db: AsyncSession, owner: str, origin_ref: str
) -> KnowledgeItem | None:
    """按来源引用查活跃素材（如 content_id）。

    队列「存入知识库」复用成稿时，origin_ref 就是 content_id。同一篇成稿
    不应存出两份，所以录入前先查有没有活跃的同源素材，有就复用。
    """
    if not origin_ref:
        return None
    rows = (
        await db.execute(
            select(KnowledgeItem).where(
                KnowledgeItem.owner == owner,
                KnowledgeItem.origin_ref == origin_ref,
                KnowledgeItem.source == "content",
                KnowledgeItem.status.in_(_ACTIVE_STATUSES),
            )
        )
    ).scalars().all()
    return rows[0] if rows else None


async def origin_map(
    db: AsyncSession, owner: str, content_ids: list[str]
) -> dict[str, str]:
    """content_id -> 知识库素材 id 的映射（仅活跃素材）。

    队列页用它一次性标出「哪些成稿已经存进知识库了」，从而把「存入」按钮
    替换成「已存入 / 查看知识库」，避免同一条任务被反复存。
    """
    ids = [c for c in (content_ids or []) if c]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(KnowledgeItem.id, KnowledgeItem.origin_ref).where(
                KnowledgeItem.owner == owner,
                KnowledgeItem.origin_ref.in_(ids),
                KnowledgeItem.source == "content",
                KnowledgeItem.status.in_(_ACTIVE_STATUSES),
            )
        )
    ).all()
    return {r.origin_ref: r.id for r in rows if r.origin_ref}


async def get_active_by_file_hash(
    db: AsyncSession, owner: str, file_hash: str
) -> KnowledgeItem | None:
    """按文件 sha256 查活跃的上传素材，避免同一文件被反复上传成多份。

    与 get_active_by_origin 同思路，区别在 key 落在 meta.file_hash 上
    （上传来源用 hash 做幂等，而不是 content_id）。

    为什么在 Python 里比对而非 SQL JSON 路径：meta 列的 typing 是
    `dict | None`，跨方言的 JSON 路径索引行为不稳，而单 owner 的上传素材
    数量本来就少，拉出来在内存里比对既稳妥又不会慢。
    """
    if not file_hash:
        return None
    rows = (
        await db.execute(
            select(KnowledgeItem).where(
                KnowledgeItem.owner == owner,
                KnowledgeItem.source == "upload",
                KnowledgeItem.status.in_(_ACTIVE_STATUSES),
            )
        )
    ).scalars().all()
    for r in rows:
        if (r.meta or {}).get("file_hash") == file_hash:
            return r
    return None


def _auto_title(text: str) -> str:
    """没给标题时，取首行/首句的前 30 字（去掉 Markdown 井号）。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    first = re.sub(r"^#{1,6}\s*", "", first).strip()
    first = re.sub(r"^>\s*", "", first).strip()
    return (first[:30] or "未命名素材").strip()


# ---------------------------------------------------------------- 后台向量化

_active: dict[str, asyncio.Task] = {}
# 单批最多重试次数（指数退避 1s / 2s），与 chunk 级重试上限（3 次）不同：
# 这里是「一次入库流程内的网络抖动」，那里是「用户手动点了重试」。
_BATCH_RETRIES = 2
_CHUNK_MAX_RETRY = 3


async def _embed_task(item_id: str) -> None:
    """把一条素材的 pending 片段全部向量化。

    全程自开 session；任何异常都记在 item.error 上，不让任务静默消失。
    """
    async with SessionLocal() as db:
        item = await db.get(KnowledgeItem, item_id)
        if not item:
            return
        rows = (
            await db.execute(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.item_id == item_id)
                .order_by(KnowledgeChunk.seq)
            )
        ).scalars().all()
        todo = [c for c in rows if c.status != "ready"]
        if not todo:
            item.status = "ready"
            item.updated_at = int(time.time())
            await db.commit()
            return

        item.status = "processing"
        item.error = ""
        item.updated_at = int(time.time())
        await db.commit()

        if not await embedding.ready(item.owner, db):
            for c in todo:
                c.status = "failed"
                c.error = "未配置任何可用的向量化模型"
            item.status = "failed"
            item.error = "未配置向量化模型（检索将退化为关键词匹配）"
            item.updated_at = int(time.time())
            await db.commit()
            invalidate_cache(item.owner)
            return

        batch = max(1, settings.KB_EMBED_BATCH)
        failed = 0
        last_err = ""
        for start in range(0, len(todo), batch):
            group = todo[start : start + batch]
            texts = [c.content for c in group]
            vecs: list[list[float]] | None = None
            used_model = ""
            for attempt in range(_BATCH_RETRIES + 1):
                try:
                    # 用 embed_with_model：模型名必须是**实际算出这批向量的那个**，
                    # 光看配置不行（池里可能临时切了备用、也可能降级到 .env 通道）
                    vecs, used_model = await embedding.embed_with_model(
                        texts, owner=item.owner, db=db
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 后台任务吞所有异常
                    last_err = str(exc)
                    if attempt < _BATCH_RETRIES:
                        await asyncio.sleep(2**attempt)
                    logger.warning(
                        "知识库向量化失败（第 %d 次）：%s", attempt + 1, last_err
                    )
            if vecs is None:
                for c in group:
                    c.status = "failed"
                    c.error = last_err[:500]
                    c.retry_count += 1
                failed += len(group)
                continue
            for c, v in zip(group, vecs):
                c.vector = v
                c.norm = embedding.norm_of(v)
                c.embed_model = used_model
                c.status = "ready"
                c.error = ""
            await db.commit()

        item.chunk_count = len(rows)
        if failed == 0:
            item.status = "ready"
            item.error = ""
        elif failed < len(todo):
            # 部分成功：仍然可检索（命中的是已就绪的那部分），但列表上要看得出来
            item.status = "partial"
            item.error = f"{failed}/{len(todo)} 个片段向量化失败，可在列表页重试"
        else:
            item.status = "failed"
            item.error = f"向量化失败：{last_err[:300]}"
        item.updated_at = int(time.time())
        await db.commit()
        invalidate_cache(item.owner)


async def retry_item(db: AsyncSession, owner: str, item_id: str) -> KnowledgeItem:
    """重跑某条素材的失败片段（用户点「重试」）。

    超过 _CHUNK_MAX_RETRY 次的片段不再重试——反复重试只会在 CF 额度打满时
    把额度烧干，用户该先去查凭证/额度。
    """
    item = await db.get(KnowledgeItem, item_id)
    if not item or item.owner != owner:
        raise KnowledgeError("素材不存在", 404)
    rows = (
        await db.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.item_id == item_id)
        )
    ).scalars().all()
    stuck = [c for c in rows if c.status != "ready" and c.retry_count < _CHUNK_MAX_RETRY]
    if not stuck:
        raise KnowledgeError("没有可重试的片段（可能已超过重试上限）")
    for c in stuck:
        c.status = "pending"
        c.error = ""
    item.status = "pending"
    item.updated_at = int(time.time())
    await db.commit()
    invalidate_cache(owner)
    task = asyncio.create_task(_embed_task(item_id))
    _active[item_id] = task
    task.add_done_callback(lambda _t, _id=item_id: _active.pop(_id, None))
    return item


async def reembed_failed_on_startup() -> int:
    """启动时把上次进程中断遗留的 pending 片段重新排队。

    只处理 pending（正常中断），不处理 failed（那是明确失败，等用户决策）。
    """
    async with SessionLocal() as db:
        ids = (
            await db.execute(
                select(KnowledgeItem.id)
                .where(KnowledgeItem.status.in_(("pending", "processing")))
                .limit(50)
            )
        ).scalars().all()
        if not ids:
            return 0
        for i in ids:
            task = asyncio.create_task(_embed_task(i))
            _active[i] = task
            task.add_done_callback(lambda _t, _id=i: _active.pop(_id, None))
        logger.info("知识库启动自愈：重新排队 %d 条素材", len(ids))
        return len(ids)


# ---------------------------------------------------------------- 检索

# 进程内向量缓存：(owner, embed_model) -> 片段列表
# 3000 片 × 1024 维 ≈ 30MB 内存，够用；写时按 owner 整桶失效。
# **缓存键必须带模型**：同一个 owner 换过 embedding 模型后，库里会同时存在
# 两个「空间」的向量，混在一份缓存里等于拿 768 维的去比 1024 维的。
_VEC_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}
# 上一轮召回缓存：(owner, embed_model) -> {"vec":..., "norm":..., "hits":[...]}
# 连续追问（「再详细点」）与上轮 query 极度相似时直接复用，省一次 embedding。
# 同样按模型隔离——换了模型还复用上轮的 query 向量，相似度毫无意义。
_LAST_QUERY: dict[tuple[str, str], dict[str, Any]] = {}


def invalidate_cache(owner: str | None = None) -> None:
    """写操作后失效缓存；owner 为空则全清。"""
    if owner is None:
        _VEC_CACHE.clear()
        _LAST_QUERY.clear()
        return
    for key in [k for k in _VEC_CACHE if k[0] == owner]:
        _VEC_CACHE.pop(key, None)
    for key in [k for k in _LAST_QUERY if k[0] == owner]:
        _LAST_QUERY.pop(key, None)


async def _load_vectors(
    db: AsyncSession, owner: str, embed_model: str = ""
) -> list[dict[str, Any]]:
    """加载**当前 embedding 模型产出的**就绪片段。

    这一行 `WHERE embed_model == 当前模型` 是换模型时的保命符：不同模型（哪怕
    同一模型的不同维度）产出的向量不在同一空间里，混着比余弦相似度，返回值
    是纯噪声——而且**不报错**，表现为「知识库突然什么都搜不到」，极难排查。
    宁可漏召回，也不能给出错误召回。
    """
    key = (owner, embed_model or "*")
    cached = _VEC_CACHE.get(key)
    if cached is not None:
        return cached
    q = (
        select(KnowledgeChunk, KnowledgeItem.title)
        .join(KnowledgeItem, KnowledgeItem.id == KnowledgeChunk.item_id)
        .where(KnowledgeChunk.owner == owner, KnowledgeChunk.status == "ready")
    )
    if embed_model:
        q = q.where(KnowledgeChunk.embed_model == embed_model)
    rows = (await db.execute(q)).all()
    out: list[dict[str, Any]] = []
    for chunk, title in rows:
        v = chunk.vector
        if not isinstance(v, list) or not v:
            continue
        out.append(
            {
                "id": chunk.id,
                "item_id": chunk.item_id,
                "title": title or "",
                "content": chunk.content,
                "vector": v,
                "norm": chunk.norm or embedding.norm_of(v),
            }
        )
    _VEC_CACHE[key] = out
    return out


async def _load_texts(db: AsyncSession, owner: str) -> list[dict[str, Any]]:
    """加载全部就绪片段的**文本**（不加载向量，也不按模型过滤）。

    关键词检索走这条路：它比的是字面重合度，跟 embedding 模型无关，所以换
    模型后仍然可用——这是「重建索引期间知识库不至于全瘫」的兜底。
    """
    rows = (
        await db.execute(
            select(KnowledgeChunk, KnowledgeItem.title)
            .join(KnowledgeItem, KnowledgeItem.id == KnowledgeChunk.item_id)
            .where(KnowledgeChunk.owner == owner, KnowledgeChunk.status == "ready")
        )
    ).all()
    return [
        {
            "id": chunk.id,
            "item_id": chunk.item_id,
            "title": title or "",
            "content": chunk.content,
        }
        for chunk, title in rows
    ]


def _keyword_hits(query: str, text: str) -> tuple[int, int]:
    """字符 bigram 重合：中文没有空格，二字组是最省事的语言无关近似。

    返回 (命中个数, query 的二字组总数)。score 取「命中比例」只用于**排序**，
    是否算命中看**命中个数** —— 长问句命中 1 个关键词比例很低（6 个二字组
    命中 1 个才 0.17），但那往往是唯一且关键的线索，按比例卡阈值会全漏。
    """
    q = re.sub(r"\s+", "", query)
    t = re.sub(r"\s+", "", text)
    if not q or not t:
        return 0, 0
    grams = {q[i : i + 2] for i in range(len(q) - 1)} or {q}
    hit = sum(1 for g in grams if g in t)
    return hit, len(grams)


def _keyword_search(rows: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for r in rows:
        hit, total = _keyword_hits(query, r["content"])
        if hit < settings.KB_MIN_KEYWORD_HITS:
            continue
        scored.append(
            {
                **{k: r[k] for k in ("id", "item_id", "title", "content")},
                "score": (hit / total) if total else 0.0,
                "matched": hit,
                "match": "keyword",
            }
        )
    scored.sort(key=lambda s: (-s["score"], s["item_id"], s["id"]))
    return scored[:top_k]


async def search(
    db: AsyncSession,
    owner: str,
    query: str,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """语义检索该 owner 的素材片段。

    降级链：向量检索 → 关键词检索 → 空列表。**永不抛异常**（调用方是写稿链路）。
    返回元素含 `score`（余弦或关键词重合度）与 `match`（`vector`/`keyword`）。
    """
    top_k = top_k or settings.KB_TOP_K
    query = (query or "").strip()
    if not query:
        return []

    # 关键词检索的素材**不按模型过滤**：换 embedding 模型后旧向量全部作废，
    # 但文本还在，字面重合度照比不误 —— 重建索引期间知识库不至于全瘫。
    try:
        texts_rows = await _load_texts(db, owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库片段加载失败：%s", exc)
        return []
    if not texts_rows:
        return []

    model = ""
    try:
        if await embedding.ready(owner, db):
            model = await embedding.current_model(owner, db)
            rows = await _load_vectors(db, owner, model)
            if not rows:
                # 当前模型下还没有任何向量（刚换模型 / 刚清空）——只能走关键词
                logger.debug("模型 %s 下暂无可用向量，降级关键词检索", model)
                return _keyword_search(texts_rows, query, top_k)
            qvec, qnorm = await embedding.embed_one(query, owner=owner, db=db), 0.0
            qnorm = embedding.norm_of(qvec)
            cache_key = (owner, model)

            # 与上轮 query 极相似（余弦 > 0.95）直接复用结果：省一次 embedding
            # 调用，也让「再详细点」这类追问拿到稳定一致的素材。
            # 缓存按 (owner, model) 隔离，换模型后不会误复用上轮。
            last = _LAST_QUERY.get(cache_key)
            if last and last.get("vec") and len(last["vec"]) == len(qvec):
                sim = embedding.cosine(last["vec"], last["norm"], qvec, qnorm)
                if sim > 0.95 and last.get("hits"):
                    return last["hits"][:top_k]
            scored = [
                {
                    **{k: r[k] for k in ("id", "item_id", "title", "content")},
                    "score": embedding.cosine(qvec, qnorm, r["vector"], r["norm"]),
                    "match": "vector",
                }
                for r in rows
            ]
            scored = [s for s in scored if s["score"] >= settings.KB_MIN_SCORE]
            scored.sort(key=lambda s: -s["score"])
            hits = scored[:top_k]
            _LAST_QUERY[cache_key] = {"vec": qvec, "norm": qnorm, "hits": hits}
            return hits
    except Exception as exc:  # noqa: BLE001 - 检索是旁路，失败就降级
        logger.warning("知识库向量检索失败，降级为关键词：%s", exc)
        if model:
            _LAST_QUERY.pop((owner, model), None)

    # 阈值已经在 _keyword_search 里按「命中二字组个数」卡过（见 config 说明）
    return _keyword_search(texts_rows, query, top_k)


def _snippet(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（已截断）"


def build_snippets(
    hits: list[dict[str, Any]], max_chars: int, per_snippet: int | None = None
) -> str:
    """把命中片段拼成注入上下文用的文本。

    预算是**硬顶**：按条累加，装不下就停（不截断半条，宁可少给一条）。
    单条超过 per_snippet 才截断——不然一条长素材就能吃掉整个预算。
    """
    per = per_snippet or settings.KB_SNIPPET_MAX_CHARS
    parts: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        body = _snippet(h.get("content") or "", per)
        title = (h.get("title") or "").strip()
        block = f"[{i}] {title}\n{body}" if title else f"[{i}] {body}"
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block) + 2
        if used >= max_chars:
            break
    return "\n\n".join(parts)


async def recall_for_prompt(
    db: AsyncSession,
    owner: str,
    query: str,
    *,
    max_chars: int | None = None,
    top_k: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """写稿前召回：返回 (注入文本, 命中明细)。

    调用方（routers/chat.py）用命中明细做「本轮用了哪些素材」埋点，
    也用它判断要不要白花 token 注入。
    """
    if len((query or "").strip()) < settings.KB_MIN_QUERY_LEN:
        # 「继续」「再长一点」这类短追问没必要检索
        return "", []
    hits = await search(db, owner, query, top_k=top_k)
    if not hits:
        return "", []
    budget = max_chars or settings.KB_MAX_CHARS
    return build_snippets(hits, budget), hits


# ---------------------------------------------------------------- 删除 / 查询

async def delete_item(db: AsyncSession, owner: str, item_id: str) -> bool:
    """删素材及其全部片段，连同上传的原始文件（级联用手动做，SQLite 不配外键）。

    原文件必须一起删：用户点「删除」的语义是「这份素材彻底没了」，
    留下孤儿文件既占盘，也会让「重新上传同一文件」时 hash 命中的历史
    元数据对不上（虽然去重只认活跃素材，但脏文件越积越多没法清理）。
    """
    item = await db.get(KnowledgeItem, item_id)
    if not item or item.owner != owner:
        return False
    file_path = item.file_path  # 先记下：delete 之后对象属性可能失效
    rows = (
        await db.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.item_id == item_id)
        )
    ).scalars().all()
    for c in rows:
        await db.delete(c)
    await db.delete(item)
    await db.commit()
    invalidate_cache(owner)

    if file_path:
        try:
            from pathlib import Path

            full = (Path(settings.UPLOAD_DIR) / file_path).resolve()
            root = Path(settings.UPLOAD_DIR).resolve()
            # 守卫：只删 UPLOAD_DIR 内的文件，防止 file_path 被写坏成绝对路径越界删除
            if root in full.parents and full.is_file():
                full.unlink()
        except Exception as exc:  # noqa: BLE001 - 删文件失败不该让删素材回滚
            logger.warning("删除素材原文件失败（素材记录已删）: %s", exc)
    return True


async def item_stats(db: AsyncSession, owner: str) -> dict[str, Any]:
    """知识库概览：条数、片数、就绪片数、是否可用向量检索。

    `stale_chunks` 是关键信号：**不等于 0 就说明换过 embedding 模型，这批
    片段的向量已经不在当前检索空间里了**，检索会静默漏掉它们——必须重建。
    """
    total_items = await _count_items(db, owner)
    total_chunks = await _count_chunks(db, owner)
    r = await db.execute(
        select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.owner == owner, KnowledgeChunk.status == "ready"
        )
    )
    ready_chunks = int(r.scalar() or 0)

    vector_ready = await embedding.ready(owner, db)
    model = await embedding.current_model(owner, db) if vector_ready else ""
    stale = await count_stale(db, owner, model) if model else 0
    return {
        "items": total_items,
        "chunks": total_chunks,
        "ready_chunks": ready_chunks,
        "pending_chunks": total_chunks - ready_chunks,
        "items_limit": settings.KB_MAX_ITEMS_PER_USER,
        "chunks_limit": settings.KB_MAX_CHUNKS_PER_USER,
        "vector_ready": vector_ready,
        "embed_model": model,
        "stale_chunks": stale,
    }


async def count_stale(db: AsyncSession, owner: str, model: str) -> int:
    """「已就绪但向量不属于当前模型」的片段数 —— 换模型后必须重建的那批。"""
    if not model:
        return 0
    r = await db.execute(
        select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.owner == owner,
            KnowledgeChunk.status == "ready",
            KnowledgeChunk.embed_model != model,
        )
    )
    return int(r.scalar() or 0)


async def reembed_stale(db: AsyncSession, owner: str) -> dict[str, Any]:
    """把「模型过期」的片段全部置回 pending 并重新排队。

    换 embedding 模型后必须走这一步，否则旧向量永远躺在库里、检索永远
    搜不到它们（不报错，只是静默漏召回——最难排查的那种故障）。

    返回 `{queued, items}`，供端点回给用户「已重新向量化 N 条素材」。
    """
    model = await embedding.current_model(owner, db)
    if not await embedding.ready(owner, db):
        raise KnowledgeError("没有可用的向量化模型，无法重建索引")
    rows = (
        await db.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.owner == owner,
                KnowledgeChunk.status == "ready",
                KnowledgeChunk.embed_model != model,
            )
        )
    ).scalars().all()
    if not rows:
        return {"queued": 0, "items": 0, "model": model}

    item_ids = {c.item_id for c in rows}
    for c in rows:
        c.status = "pending"
        c.error = ""
        c.retry_count = 0
    await db.commit()

    # 父素材状态回退：不然列表页还显示「已就绪」，用户以为没事
    for iid in item_ids:
        item = await db.get(KnowledgeItem, iid)
        if item and item.owner == owner:
            item.status = "pending"
            item.error = ""
            item.updated_at = int(time.time())
    await db.commit()
    invalidate_cache(owner)

    for iid in item_ids:
        task = asyncio.create_task(_embed_task(iid))
        _active[iid] = task
        task.add_done_callback(lambda _t, _id=iid: _active.pop(_id, None))

    logger.info("重建过期向量: owner=%s model=%s 片段=%d 素材=%d",
                owner, model, len(rows), len(item_ids))
    return {"queued": len(rows), "items": len(item_ids), "model": model}
