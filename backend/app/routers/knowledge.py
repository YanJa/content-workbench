"""知识库（RAG 素材）路由。

权限口径（与知乎热点同一套思路：读宽、写严）：

| 端点 | 权限 | 理由 |
|---|---|---|
| `GET /items`、`GET /stats`、`POST /search` | 登录即可 | 检索就在写稿链路上，收窄成管理员会让普通用户直接失去核心功能 |
| `POST /items`、`POST /items/{id}/retry` | `knowledge.create` | 会消耗 Cloudflare 向量化额度，是真金白银 |
| `PATCH /items/{id}`、`DELETE /items/{id}` | `knowledge.delete` | 删素材连带删向量，不可恢复 |

所有端点按 owner 严格隔离：素材是「先做私有」的定位，跨用户既读不到也删不掉。
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import current_user, require_use
from app.core.tenant import current_owner_id
from app.core.config import settings
from app.db.session import get_db
from app.models.content import Content
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.models.user import User
from app.schemas.models import (
    KnowledgeCreate,
    KnowledgeSearchRequest,
    KnowledgeUpdate,
)
from app.services import knowledge as kb
from app.services import doc_extract as doc_extract

router = APIRouter(prefix="/knowledge", tags=["知识库"])


def _out(item: KnowledgeItem, with_body: bool = False) -> dict:
    d = {
        "id": item.id,
        "source": item.source,
        "title": item.title,
        "origin_ref": item.origin_ref,
        "status": item.status,
        "chunk_count": item.chunk_count,
        "error": item.error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "char_count": len(item.raw_text or ""),
    }
    if with_body:
        d["raw_text"] = item.raw_text
    return d


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """知识库概览：条数 / 片数 / 向量化通道是否就绪。"""
    return await kb.item_stats(db, owner)


@router.get("/origin-check")
async def origin_check(
    content_ids: list[str] = Query(default=[], description="成稿 content_id 列表（可重复传）"),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """批量查「哪些成稿已存进知识库」。

    队列页用来一次性标出已存行：命中返回 `{"<content_id>": "<item_id>"}`，
    未存的不出现在 map 里。前端据此把「存入知识库」按钮换成「已存入 / 查看」，
    杜绝同一篇成稿被反复存。只读、登录即可。
    """
    return await kb.origin_map(db, owner, content_ids)


@router.get("/items")
async def list_items(
    q: str = Query("", description="搜索：标题或原文"),
    source: str = Query("all", description="manual/content/upload/url，all=全部"),
    status: str = Query("all", description="pending/processing/ready/partial/failed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """素材列表（分页 + 搜索 + 来源筛选，全在后端做）。"""
    conds = [KnowledgeItem.owner == owner]
    if source and source != "all":
        conds.append(KnowledgeItem.source == source)
    if status and status != "all":
        conds.append(KnowledgeItem.status == status)
    if q:
        like = f"%{q}%"
        conds.append(or_(KnowledgeItem.title.ilike(like), KnowledgeItem.raw_text.ilike(like)))

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(KnowledgeItem).where(*conds)
            )
        ).scalar()
        or 0
    )
    rows = (
        await db.execute(
            select(KnowledgeItem)
            .where(*conds)
            .order_by(desc(KnowledgeItem.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "items": [_out(i) for i in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.post("/items")
async def create(
    req: KnowledgeCreate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("knowledge.create")),
):
    """新增素材：手动录入，或复用工作台已产成稿。

    `content_id` 那条路取的是 `Content.body`（结构化 Markdown），
    **不取** `body_html` —— 后者全是内联样式噪声，喂给模型只会污染上下文。
    """
    text = (req.text or "").strip()
    origin_ref = req.origin_ref or ""
    title = (req.title or "").strip()

    if not text and req.content_id:
        content = await db.get(Content, req.content_id)
        if not content or content.owner != owner:
            raise HTTPException(404, "成稿不存在")
        text = (content.body or content.summary or "").strip()
        if not text:
            raise HTTPException(400, "该成稿没有可存入的正文内容")
        title = title or content.title
        origin_ref = origin_ref or content.id

    source = req.source if req.source in ("manual", "content", "upload", "url") else "manual"
    if req.content_id:
        source = "content"

    # 复用成稿时，同一篇不该存出两份：先查有没有活跃同源素材，有就复用。
    if origin_ref:
        existing = await kb.get_active_by_origin(db, owner, origin_ref)
        if existing:
            return {"ok": True, "item": _out(existing), "already_exists": True}

    try:
        item = await kb.create_item(
            db,
            owner,
            source=source,
            title=title,
            raw_text=text,
            origin_ref=origin_ref,
        )
    except kb.KnowledgeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True, "item": _out(item), "already_exists": False}


@router.post("/upload")
async def upload(
    file: UploadFile = File(..., description="待入库文档"),
    title: str = Form(""),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("knowledge.create")),
):
    """上传文档入库（source=upload）。

    支持 txt/md/markdown/json/pdf/doc/docx。流程：
      1. 校验扩展名（不在白名单直接拒）与大小（见 KB_UPLOAD_MAX_BYTES）；
      2. 落盘到 `UPLOAD_DIR/kb/<owner>/<uid>.<ext>`，保留原文件备查；
      3. 按扩展名抽纯文本（doc_extract 负责，失败给中文提示）；
      4. 同一文件（sha256）已存在则复用，不重复入库；
      5. 交 create_item 切片 + 后台向量化。
    """
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in doc_extract.ALLOWED_EXTS:
        raise HTTPException(
            400,
            f"不支持的文件类型{('.' + ext) if ext else ''}。支持：{', '.join(sorted(doc_extract.ALLOWED_EXTS))}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > doc_extract.KB_UPLOAD_MAX_BYTES:
        mb = doc_extract.KB_UPLOAD_MAX_BYTES // (1024 * 1024)
        raise HTTPException(400, f"文件过大（上限 {mb}MB）")

    import hashlib

    file_hash = hashlib.sha256(raw).hexdigest()

    # 同文件幂等：已存在活跃素材就复用（用户重复上传不会制造多份）
    existing = await kb.get_active_by_file_hash(db, owner, file_hash)
    if existing:
        return {"ok": True, "item": _out(existing), "already_exists": True}

    # 落盘原文件
    from pathlib import Path

    kb_dir = Path(settings.UPLOAD_DIR) / "kb" / owner
    kb_dir.mkdir(parents=True, exist_ok=True)
    saved_path = kb_dir / f"{uuid.uuid4().hex}.{ext}"
    saved_path.write_bytes(raw)

    text, err = doc_extract.extract_text(str(saved_path), filename)
    if err:
        # 抽不出来就别留半截文件
        saved_path.unlink(missing_ok=True)
        raise HTTPException(400, err)
    if not text.strip():
        saved_path.unlink(missing_ok=True)
        raise HTTPException(400, "文件里没有可提取的文字（可能是扫描版 PDF 或空文件）")

    try:
        item = await kb.create_item(
            db,
            owner,
            source="upload",
            title=(title or "").strip() or Path(filename).stem,
            raw_text=text,
            file_path=str(saved_path.relative_to(Path(settings.UPLOAD_DIR))),
            meta={"file_hash": file_hash, "filename": filename},
        )
    except kb.KnowledgeError as exc:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True, "item": _out(item), "already_exists": False}


@router.get("/items/{item_id}")
async def detail(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    item = await db.get(KnowledgeItem, item_id)
    if not item or item.owner != owner:
        raise HTTPException(404, "素材不存在")
    chunks = (
        await db.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.item_id == item_id)
            .order_by(KnowledgeChunk.seq)
        )
    ).scalars().all()
    return {
        **_out(item, with_body=True),
        "chunks": [
            {
                "id": c.id,
                "seq": c.seq,
                "content": c.content,
                "char_len": c.char_len,
                "status": c.status,
                "error": c.error,
            }
            for c in chunks
        ],
    }


@router.patch("/items/{item_id}")
async def update(
    item_id: str,
    req: KnowledgeUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("knowledge.delete")),
):
    """改标题（内容不支持改：改了就得重切重算向量，等于删了重加）。"""
    item = await db.get(KnowledgeItem, item_id)
    if not item or item.owner != owner:
        raise HTTPException(404, "素材不存在")
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    item.title = title[:300]
    item.updated_at = int(time.time())
    await db.commit()
    return {"ok": True, "item": _out(item)}


@router.delete("/items/{item_id}")
async def delete(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("knowledge.delete")),
):
    """删素材及其全部片段。"""
    ok = await kb.delete_item(db, owner, item_id)
    if not ok:
        raise HTTPException(404, "素材不存在")
    return {"ok": True}


@router.post("/items/{item_id}/retry")
async def retry(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("knowledge.create")),
):
    """重跑失败/中断的片段（额度打满、网络抖动后的补救入口）。"""
    try:
        item = await kb.retry_item(db, owner, item_id)
    except kb.KnowledgeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True, "item": _out(item)}


@router.post("/search")
async def search(
    req: KnowledgeSearchRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _user: User = Depends(current_user),
):
    """召回调试：看一句话能命中哪些素材片段。

    与写稿时走的是同一个 `kb.search()`，只是把分数与命中方式（vector/keyword）
    摊开给前端，方便判断「为什么没召回」。
    """
    hits = await kb.search(db, owner, req.query, top_k=req.top_k)
    return {
        "query": req.query,
        "total": len(hits),
        "hits": [
            {
                "item_id": h["item_id"],
                "title": h["title"],
                "content": h["content"][:300],
                "score": round(h["score"], 4),
                "match": h["match"],
            }
            for h in hits
        ],
    }
