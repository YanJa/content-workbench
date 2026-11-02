"""内容与发布队列路由。"""
from __future__ import annotations

import uuid
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.models.content import Content, QueueItem
from app.prompts.platforms import normalize_sub_type
from app.services.zhihu import publish_url as zhihu_publish_url
from app.schemas.models import (
    ChecklistUpdate,
    ContentCreateRequest,
    ContentOut,
    ContentUpdateRequest,
    PlaintextUpdateRequest,
    QueueItemOut,
    QueueStatusUpdate,
    TraceSnapshotIn,
)

router = APIRouter(prefix="/queue", tags=["发布队列"])


# 标题长度上限。前端输入框 maxlength 同步这个值；后端不静默截断 —— 超长直接
# 报错，不然「前端说 64、后端其实截 300」就是又一处规则漂移。
_TITLE_MAX = 64


def _title_editable(item: QueueItem, layout_status: str, processing: str) -> bool:
    """标题现在还能改吗？

    「未排版之前可以改」—— 这是唯一一处规则，列表接口和 PATCH 都用它，
    前端只读这个结果，不自己复刻（复刻迟早漂移，变成「按钮在、点了 409」）。

    不能改的四种情况：
      - 已经排过版（layout_status != none）：标题已经进了 HTML / 封面
      - 已经推到公众号草稿箱：改了本地标题跟微信侧对不上
      - 正在加工（排版/生图中）：并发写会互相覆盖
      - 已发布 / 已归档：事后再改没有意义
    """
    if item.wechat_media_id:
        return False
    if processing:
        return False
    if (layout_status or "none") != "none":
        return False
    return item.status in ("draft", "ready")


def _publish_url(platform: str, sub_type: str, meta: dict | None) -> str:
    """这条内容该去哪儿发布。

    只有知乎需要：官方没有发布接口，系统给的是「人带着内容去发」的网页入口。
    公众号走草稿箱推送、小红书/抖音本来就是复制带走，都不填这个字段。
    """
    if platform != "zhihu":
        return ""
    return zhihu_publish_url(sub_type, (meta or {}).get("zhihu_question", ""))


@router.get("/items")
async def list_items(
    status: str = Query("all", description="状态筛选，逗号分隔可多选；all=全部"),
    platform: str = Query("all"),
    q: str = Query("", description="模糊搜索：匹配标题或正文（不区分大小写）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """分页返回发布队列。

    返回 `{items, total, page, page_size}`。筛选（status/platform/q）在**后端**做，
    前端不能再本地过滤（否则每页条数对不上）。各筛选之间是 **AND** 关系：
    状态多选 + 标题/正文搜索同时生效。数据量增长后靠分页控制单次查询与渲染量。
    """
    conds = [QueueItem.owner == owner]
    if status and status != "all":
        statuses = [s for s in status.split(",") if s]
        if statuses:
            conds.append(QueueItem.status.in_(statuses))
    if platform != "all":
        conds.append(QueueItem.platform == platform)

    # 模糊搜索：标题 OR 正文（正文在关联的 Content 表）。带 q 时才 join Content，
    # 避免无谓的连表影响性能。
    need_join = bool(q)
    if need_join:
        like = f"%{q}%"
        conds.append(
            or_(QueueItem.title.ilike(like), Content.body.ilike(like))
        )

    count_stmt = select(func.count()).select_from(QueueItem)
    if need_join:
        count_stmt = count_stmt.join(Content, Content.id == QueueItem.content_id, isouter=True)
    total = (await db.execute(count_stmt.where(*conds))).scalar_one()

    stmt = select(QueueItem)
    if need_join:
        stmt = stmt.join(Content, Content.id == QueueItem.content_id, isouter=True)
    stmt = (
        stmt.where(*conds)
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
                    Content.layout_status,
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
            layout_status = (r[4] or "none") if r else "none"
            d.layout_status = layout_status
            d.title_editable = _title_editable(it, layout_status, d.processing)
            d.publish_url = _publish_url(it.platform, d.sub_type, r[3] if r else None)
            out.append(d)
    return {"items": out, "total": total, "page": page, "page_size": page_size}


@router.get("/saved-map")
async def saved_map(
    conversation_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """查询某会话里哪些 AI 回复已保存到队列。

    返回 `{map: {msg_id: content_id}}`，对话页加载会话时据此重建
    「已入队 / 去队列查看」状态，避免刷新或跳回后状态丢失导致重复入队。
    """
    rows = (
        await db.execute(
            select(Content.id, Content.meta).where(
                Content.owner == owner,
                Content.conversation_id == conversation_id,
                Content.meta.isnot(None),
            )
        )
    ).all()
    m: dict[str, str] = {}
    for cid, meta in rows:
        mid = (meta or {}).get("msg_id")
        if mid:
            m[mid] = cid
    return {"map": m}


@router.get("/stats")
async def stats(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
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
    owner: str = Depends(current_owner_id),
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    content = await db.get(Content, item.content_id)

    # 改标题：只在未排版之前允许（规则见 _title_editable）
    if req.title is not None:
        if not req.title.strip():
            raise HTTPException(400, "标题不能为空")
        if len(req.title.strip()) > _TITLE_MAX:
            raise HTTPException(400, f"标题最长 {_TITLE_MAX} 字")
        if req.title.strip() == (item.title or ""):
            req.title = None  # 没变，省一次校验
    if req.title is not None:
        layout_status = (content.layout_status if content else None) or "none"
        processing = (content.processing if content else None) or ""
        if not _title_editable(item, layout_status, processing):
            raise HTTPException(409, "已排版（或已推送/加工中）的队列项不能改标题")
        item.title = req.title.strip()
        # 同步 Content.title：加工面板显示的是它，不同步的话用户改完队列标题，
        # 点开「加工排版」看到的还是旧标题，会以为没生效。
        if content is not None:
            content.title = item.title

    if req.status:
        # 放弃只允许从「未发布」状态进入：已发布/已归档是完结态、已放弃本身是放弃态，
        # 都不该再被标记放弃（与前端口径一致：只有待加工/待发布能放弃）。
        if req.status == "abandoned" and item.status in ("published", "archived", "abandoned"):
            raise HTTPException(400, "已发布、已归档或已放弃的内容不能再放弃")
        item.status = req.status
        if req.status == "published":
            item.published_at = int(time.time())
    await db.flush()
    out = QueueItemOut.model_validate(item)
    # 补发布深链：PATCH 之后前端拿这一行覆盖本地数据，不补就变成空链接了
    out.publish_url = _publish_url(item.platform, item.sub_type, content.meta if content else None)
    return out


@router.put("/items/{item_id}/checklist", response_model=QueueItemOut)
async def update_checklist(
    item_id: str,
    req: ChecklistUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    item.checklist = req.checklist
    await db.flush()
    content = await db.get(Content, item.content_id)
    out = QueueItemOut.model_validate(item)
    out.publish_url = _publish_url(item.platform, item.sub_type, content.meta if content else None)
    return out


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
):
    item = await db.get(QueueItem, item_id)
    if not item or (item.owner or "default") != owner:
        raise HTTPException(404, "队列项不存在")
    await db.delete(item)
    await db.flush()
    return {"ok": True}


@router.get("/export")
async def export_markdown(
    db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
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
    owner: str = Depends(current_owner_id),
):
    stmt = select(Content).where(Content.owner == owner)
    if platform != "all":
        stmt = stmt.where(Content.platform == platform)
    stmt = stmt.order_by(Content.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


@content_router.get("/items/{content_id}", response_model=ContentOut)
async def get_content(
    content_id: str, db: AsyncSession = Depends(get_db), owner: str = Depends(current_owner_id)
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
    owner: str = Depends(current_owner_id),
):
    """用户点「保存到队列」时调用：建内容 + 入队（状态 draft = 待加工）。

    对话创作不再自动入队（2026-10-16 起）。这里是由前端「保存到队列」按钮
    显式触发的唯一入队入口，`conversation_id` 回填以便队列项能跳回原对话。
    """
    cid = str(uuid.uuid4())
    meta: dict[str, Any] = {"source": "chat"}
    if req.msg_id:
        meta["msg_id"] = req.msg_id
    sub_type = normalize_sub_type(req.platform, req.sub_type)
    if sub_type == "answer" and req.zhihu_question.strip():
        # 归属问题：回答要挂在具体问题下，队列页据此给直达深链
        meta["zhihu_question"] = req.zhihu_question.strip()
    db.add(
        Content(
            id=cid,
            platform=req.platform,
            kind=req.kind,
            sub_type=sub_type,
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
            sub_type=sub_type,
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
    owner: str = Depends(current_owner_id),
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


@content_router.patch("/items/{content_id}/plaintext", response_model=ContentOut)
async def update_plaintext(
    content_id: str,
    req: PlaintextUpdateRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """改排版产物里的纯文本（头条号 / 百家号 / 贴图文案）。

    纯文本平台的产物就是一段字，用户排版完总要顺手改两句 —— 标题党一点、
    语气软一点。这段字只存在 `layouts[platform].text` 里，和富文本的
    `body_html` 是两条线，所以单独给一个只动这一个键的入口。

    只覆盖目标平台的 `text`，其余字段（`updated_at` 之外的产物元数据）保留：
    排版任务可能往同一个 entry 里写过别的东西，整包替换会连它们一起抹掉。
    """
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")

    layouts = dict(c.layouts or {})
    entry = dict(layouts.get(req.platform) or {})
    entry["text"] = req.text
    entry["updated_at"] = int(time.time())
    layouts[req.platform] = entry
    # 必须整体换新对象：SQLAlchemy 的 JSON 列按对象身份判脏，原地改键不会触发 UPDATE
    c.layouts = layouts
    c.updated_at = int(time.time())
    await db.flush()
    return c


@content_router.put("/items/{content_id}/trace-snapshot", response_model=ContentOut)
async def save_trace_snapshot(
    content_id: str,
    req: TraceSnapshotIn,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """把一次 AI 痕迹检测的结果存成快照，挂到 Content.meta['aigc_trace']。

    已发布内容在只读排版查看态要回看「发布当时」的检测数据，而报告单本来是
    一次性、不落库的，所以检测（体检 / 朱雀）完成后由前端把结果推回来存一份。
    按检测对象（layout / body）各存一份：发布看的是排版产物那份。

    只动 meta['aigc_trace'] 这一个键，整包替换 JSON 对象（SQLAlchemy 按对象
    身份判脏）；其余 meta（msg_id / zhihu_question 等）原样保留。
    """
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")

    now = int(time.time())
    meta = dict(c.meta or {})
    snap = dict(meta.get("aigc_trace") or {})
    snap["saved_at"] = now
    if req.platform:
        snap["platform"] = req.platform
    if req.sub_type:
        snap["sub_type"] = req.sub_type
    # 该检测对象那一份：体检/朱雀分别更新，互不打断
    slot = dict(snap.get(req.source) or {})
    slot["source"] = req.source
    if req.scan is not None:
        slot["scan"] = req.scan
    if req.zhuque is not None:
        slot["zhuque"] = req.zhuque
    snap[req.source] = slot
    meta["aigc_trace"] = snap
    # 整体换新对象，确保 JSON 列判脏触发 UPDATE
    c.meta = meta
    c.updated_at = now
    await db.flush()
    return c
