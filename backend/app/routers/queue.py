"""内容与发布队列路由。"""
from __future__ import annotations

import uuid
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import current_owner
from app.db.session import get_db
from app.models.content import Content, QueueItem
from app.schemas.models import (
    ChecklistUpdate,
    ContentCreateRequest,
    ContentOut,
    ContentUpdateRequest,
    QueueItemOut,
    QueueStatusUpdate,
)

router = APIRouter(prefix="/queue", tags=["发布队列"])


@router.get("/items")
async def list_items(
    status: str = Query("all"),
    platform: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    """分页返回发布队列。

    返回 `{items, total, page, page_size}`。筛选（status/platform）在**后端**做，
    前端不能再本地过滤（否则每页条数对不上）。数据量增长后靠分页控制单次查询与渲染量。
    """
    conds = [QueueItem.owner == owner]
    if status != "all":
        conds.append(QueueItem.status == status)
    if platform != "all":
        conds.append(QueueItem.platform == platform)

    total = (
        await db.execute(select(func.count()).select_from(QueueItem).where(*conds))
    ).scalar_one()

    stmt = (
        select(QueueItem)
        .where(*conds)
        .order_by(QueueItem.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())

    # 透传关联内容的加工中标记（processing）+ 来源会话/消息，队列页据此显示
    # 「排版中/生图中」和「回到对话」按钮。批量查一次避免 N+1。
    out: list[QueueItemOut] = []
    if items:
        cids = [it.content_id for it in items]
        rows = (
            await db.execute(
                select(
                    Content.id,
                    Content.processing,
                    Content.conversation_id,
                    Content.meta,
                ).where(Content.id.in_(cids))
            )
        ).all()
        cmap = {r[0]: r for r in rows}
        for it in items:
            r = cmap.get(it.content_id)
            d = QueueItemOut.model_validate(it)
            d.processing = (r[1] or "") if r else ""
            d.conversation_id = r[2] if r else None
            d.msg_id = ((r[3] or {}).get("msg_id") if r else None) or None
            out.append(d)
    return {"items": out, "total": total, "page": page, "page_size": page_size}


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
):
    rows = (
        await db.execute(select(QueueItem).where(QueueItem.owner == owner))
    ).scalars().all()
    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        by_platform[r.platform] = by_platform.get(r.platform, 0) + 1
    return {"total": len(rows), "by_status": by_status, "by_platform": by_platform}


@router.patch("/items/{item_id}", response_model=QueueItemOut)
async def update_status(
    item_id: str,
    req: QueueStatusUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    item.status = req.status
    if req.status == "published":
        item.published_at = int(time.time())
    await db.flush()
    return item


@router.put("/items/{item_id}/checklist", response_model=QueueItemOut)
async def update_checklist(
    item_id: str,
    req: ChecklistUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    item.checklist = req.checklist
    await db.flush()
    return item


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    await db.delete(item)
    await db.flush()
    return {"ok": True}


@router.get("/export")
async def export_markdown(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
):
    """导出发布清单（Markdown），便于同步给团队。"""
    rows = (
        await db.execute(
            select(QueueItem)
            .where(QueueItem.owner == owner)
            .order_by(QueueItem.created_at.desc())
        )
    ).scalars().all()

    labels = {"draft": "待加工", "ready": "待发布", "published": "已发布", "archived": "已归档"}
    lines = [
        "# 发布队列",
        "",
        f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| 标题 | 平台 | 状态 | 创建时间 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        ts = time.strftime("%Y-%m-%d", time.localtime(r.created_at))
        lines.append(f"| {r.title} | {r.platform} | {labels.get(r.status, r.status)} | {ts} |")
    return {"markdown": "\n".join(lines)}


# ---------------------------------------------------------------- 内容
content_router = APIRouter(prefix="/content", tags=["内容"])


@content_router.get("/items", response_model=list[ContentOut])
async def list_contents(
    platform: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    stmt = select(Content).where(Content.owner == owner)
    if platform != "all":
        stmt = stmt.where(Content.platform == platform)
    stmt = stmt.order_by(Content.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


@content_router.get("/items/{content_id}", response_model=ContentOut)
async def get_content(
    content_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner)
):
    """按 ID 取单条内容（队列页推草稿箱时需要正文）。"""
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")
    return c


@content_router.post("/items")
async def create_content(
    req: ContentCreateRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    """用户点「保存到队列」时调用：建内容 + 入队（状态 draft = 待加工）。

    对话创作不再自动入队（2026-10-16 起）。这里是由前端「保存到队列」按钮
    显式触发的唯一入队入口，`conversation_id` 回填以便队列项能跳回原对话。
    """
    cid = str(uuid.uuid4())
    meta: dict[str, Any] = {"source": "chat"}
    if req.msg_id:
        meta["msg_id"] = req.msg_id
    db.add(
        Content(
            id=cid,
            platform=req.platform,
            kind=req.kind,
            title=req.title[:300],
            body=req.body,
            summary=req.summary,
            owner=owner,
            conversation_id=req.conversation_id,
            meta=meta,
        )
    )
    item_id = str(uuid.uuid4())
    db.add(
        QueueItem(
            id=item_id,
            content_id=cid,
            platform=req.platform,
            title=req.title[:300],
            # draft = 待加工：用户到队列页做排版/配图后才变 ready
            status="draft",
            owner=owner,
        )
    )
    await db.flush()
    return {"ok": True, "content_id": cid, "queue_item_id": item_id}


@content_router.patch("/items/{content_id}", response_model=ContentOut)
async def update_content(
    content_id: str,
    req: ContentUpdateRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner),
):
    """部分更新内容（加工排版面板里编辑正文 / 排版 HTML 后保存）。

    只更新显式传入的字段：`None` = 不改 —— 这样前端能只提交真的要改的字段，
    不会被漏传的字段覆盖成空串。
    """
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")
    if req.title is not None:
        c.title = req.title[:300]
    if req.body is not None:
        c.body = req.body
    if req.summary is not None:
        c.summary = req.summary
    if req.body_html is not None:
        c.body_html = req.body_html
    c.updated_at = int(time.time())
    await db.flush()
    return c
