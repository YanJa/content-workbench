"""素材库接口。

两个安全取向，写在最前面：

1. **导入时不信任前端给的 URL**。`/assets/import` 只收 `source_name + source_id`，
   由后端回源站重新查一次拿到真实下载地址。否则这个接口就是个 SSRF 跳板
   （传 `http://127.0.0.1:8000/api/...` 或内网地址，让服务端替攻击者发请求）。

2. **文件直链走 HMAC 签名**（`link_auth`），与现有配图同一套机制。
   `<img src>` 带不了 Authorization 头，只能靠签名；签名绑 path，
   拿到一个 URL 只能读那一个文件。
"""
from __future__ import annotations

import mimetypes
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tenant import current_owner_id, link_auth
from app.db.session import get_db
from app.models.asset import Asset
from app.models.content import Content
from app.services import assets as svc
from app.services import asset_sources
from app.services.assets import AssetError
from app.services.storage import get_storage

router = APIRouter(tags=["素材库"])


# ---------------------------------------------------------------- 列表与统计
@router.get("/assets")
async def list_assets(
    kind: str | None = Query(default=None, description="image / video"),
    source: str | None = Query(default=None, description="upload / search / ai / cover"),
    q: str | None = Query(default=None, description="标题或标签关键词"),
    tag: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """素材列表。按入库时间倒序 —— 用户找的通常是「刚存的那张」。"""
    cond = [Asset.owner == owner]
    if kind:
        cond.append(Asset.kind == kind)
    if source:
        cond.append(Asset.source == source)
    if q:
        like = f"%{q.strip()}%"
        # tags 是 JSON 列，SQLite 里存的是文本，LIKE 能匹配到（够用，不必上全文索引）
        cond.append(or_(Asset.title.like(like), Asset.tags.like(like)))
    if tag:
        cond.append(Asset.tags.like(f"%{tag}%"))

    total = int(
        (await db.execute(select(func.count(Asset.id)).where(*cond))).scalar() or 0
    )
    rows = (
        (
            await db.execute(
                select(Asset)
                .where(*cond)
                .order_by(Asset.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "ok": True,
        "items": [svc.asset_dict(a) for a in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/assets/stats")
async def asset_stats(
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """容量与来源分布。列表页顶部的占用条与筛选计数用。"""
    used = await svc.used_bytes(db, owner)
    total = int(
        (await db.execute(select(func.count(Asset.id)).where(Asset.owner == owner))).scalar() or 0
    )
    by_kind = dict(
        (
            await db.execute(
                select(Asset.kind, func.count(Asset.id))
                .where(Asset.owner == owner)
                .group_by(Asset.kind)
            )
        ).all()
    )
    by_source = [
        {"source": s or "", "count": int(n)}
        for s, n in (
            await db.execute(
                select(Asset.source_name, func.count(Asset.id))
                .where(Asset.owner == owner)
                .group_by(Asset.source_name)
            )
        ).all()
    ]
    return {
        "ok": True,
        "total": total,
        "image_count": int(by_kind.get("image", 0)),
        "video_count": int(by_kind.get("video", 0)),
        "used_bytes": used,
        "quota_bytes": max(1, settings.ASSET_QUOTA_MB) * 1024 * 1024,
        "by_source": by_source,
    }


# ---------------------------------------------------------------- 上传与文件
@router.post("/assets/upload")
async def upload_asset(
    file: UploadFile = File(...),
    title: str | None = None,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """上传入库。同一张图重复上传会命中已有记录，返回 `created=False`。"""
    data = await file.read()
    try:
        asset, created = await svc.save_bytes(
            db,
            owner,
            data,
            kind="image",
            source="upload",
            license="own",  # 上传默认视作自有素材；P2 去水印会据此放行
            filename=file.filename or "",
            title=title or "",
        )
    except AssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "created": created, "asset": svc.asset_dict(asset)}


@router.get("/assets/file/{key:path}")
async def serve_asset_file(
    key: str,
    owner: str = Depends(link_auth),
) -> Response:
    """素材直链。签名由 `storage.url()` 生成，这里只做读取。

    路径校验交给 StorageBackend（`assets/` 前缀 + 无 `..`），
    不在路由层重复实现一遍 —— 两处校验迟早会不一致。

    归属二次校验：素材 key 形如 `assets/<owner>/<yyyymm>/<id>.<ext>`，
    这里拦一道 `parts[1] != owner`。原因：`link_auth` 的 HMAC 分支安全的
    （签名绑 owner+path，无法伪造他人），但它的 Bearer 分支只对「你是谁」
    做校验、不确认「你能否访问这个 key」—— 漏掉这道会允许任意登录用户
    拿自己的 token 读 `assets/<他人owner>/...` 的素材原图/视频。HMAC 分支
    返回的 owner 即签名者，本校验对其无副作用。
    """
    parts = key.split("/")
    if len(parts) < 3 or parts[0] != "assets" or parts[1] != owner:
        raise HTTPException(404, "素材不存在")
    try:
        data = await get_storage().get(key)
    except (ValueError, FileNotFoundError, OSError):
        # 不区分「非法 key」和「文件不存在」：对外都是 404，不泄露目录结构
        raise HTTPException(404, "素材不存在") from None
    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        # 签名 URL 是稳定的（同一 asset 同一签名），可放心让浏览器缓存一天：
        # 素材内容不变，重复请求只会白耗带宽。
        headers={"Cache-Control": "private, max-age=86400"},
    )


# ---------------------------------------------------------------- 改与删
class AssetUpdate(BaseModel):
    title: str | None = None
    tags: list[str] | None = None


@router.patch("/assets/{asset_id}")
async def update_asset(
    asset_id: str,
    req: AssetUpdate,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    asset = await _get_asset(db, asset_id, owner)
    if req.title is not None:
        asset.title = req.title.strip()[:300]
    if req.tags is not None:
        # 去空、去重、限长：标签是给人看的，脏数据比没数据更烦
        seen: list[str] = []
        for t in req.tags:
            t = (t or "").strip()[:24]
            if t and t not in seen:
                seen.append(t)
        asset.tags = seen[:20]
    asset.updated_at = int(time.time())
    await db.commit()
    return {"ok": True, "asset": svc.asset_dict(asset)}


@router.delete("/assets/{asset_id}")
async def delete_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """删除素材。

    已插进文章的图片**不会**跟着消失：`Content.images` 里存的是当时算好的 URL 与
    path 快照，删素材会让那些引用变成死链。所以这里只提示影响面，不代用户清理 ——
    是否连带删正文引用，该由用户在队列页逐个决定，不该由一个删除按钮替他决定。
    """
    asset = await _get_asset(db, asset_id, owner)
    referenced = asset.used_count or 0
    await svc.delete_asset(db, asset)
    return {"ok": True, "referenced": referenced}


# ---------------------------------------------------------------- 在线搜索与导入
class SearchReq(BaseModel):
    q: str = Field(min_length=1, max_length=100)
    source: str = "all"  # all / pixabay / openverse
    page: int = Field(default=1, ge=1, le=20)
    per_page: int = Field(default=20, ge=1, le=50)


@router.get("/assets/sources")
async def list_sources(owner: str = Depends(current_owner_id)) -> dict[str, Any]:
    """图源可用状态。前端据此决定显示哪些 tab、以及「去配 key」的提示。

    要鉴权：这个接口会暴露「本机配了哪些 API Key」，未登录不该看得到。
    """
    return {"ok": True, "sources": asset_sources.source_status()}


@router.post("/assets/search")
async def search_assets(req: SearchReq, owner: str = Depends(current_owner_id)) -> dict[str, Any]:
    """在线搜图，**不落库**。

    搜索结果只是候选：用户可能只是看看，选中了才 import。这也顺带满足
    Pixabay「结果须缓存 24 小时」的条款要求（缓存在 asset_sources 里）。

    要鉴权：图源配额按我们这一方的 key 算，匿名调用等于把额度公开送人。

    `errors` 是要紧的：图源连不上和「这个词真没图」在用户眼里长得一样，
    但处理方式完全不同。前端据它区分「换个关键词」和「去配 key / 查网络」。
    """
    items, errors = await asset_sources.search(req.q.strip(), req.source, req.page, req.per_page)
    return {
        "ok": True,
        "items": items,
        "errors": errors,
        "sources": asset_sources.source_status(),
    }


class ImportReq(BaseModel):
    source_name: str
    source_id: str


@router.post("/assets/import")
async def import_asset(
    req: ImportReq,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """把搜索候选下载入库。

    只收 `source_name + source_id`，下载地址由后端回源站查（见文件头注释）。
    """
    # 已导入过就直接返回，不再请求源站
    existing = await svc.find_by_source(db, owner, req.source_name, req.source_id)
    if existing:
        return {"ok": True, "created": False, "asset": svc.asset_dict(existing)}

    recent = await svc.count_recent_imports(db, owner)
    if recent >= max(1, settings.ASSET_IMPORT_PER_HOUR):
        raise HTTPException(
            429,
            f"每小时最多导入 {settings.ASSET_IMPORT_PER_HOUR} 张（已导入 {recent} 张）。"
            "这是图源条款的批量下载限制，稍后再试。",
        )

    candidate = await asset_sources.fetch_one(req.source_name, req.source_id)
    if not candidate:
        raise HTTPException(404, "这张素材已从图源下架，或源站返回异常")

    data = await asset_sources.download(candidate["full_url"])
    try:
        asset, created = await svc.save_bytes(
            db,
            owner,
            data,
            kind="image",
            source="search",
            source_name=candidate["source_name"],
            source_id=candidate["source_id"],
            source_url=candidate["source_url"],
            license=candidate["license"],
            license_url=candidate["license_url"],
            author=candidate["author"],
            author_url=candidate["author_url"],
            attribution_text=candidate["attribution_text"],
            title=candidate["title"],
            tags=candidate.get("tags") or [],
            mime=candidate.get("mime") or "",
            filename=candidate["full_url"].rsplit("/", 1)[-1].split("?")[0],
        )
    except AssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "created": created, "asset": svc.asset_dict(asset)}


# ---------------------------------------------------------------- 发布链路复用
class AttachReq(BaseModel):
    asset_id: str


@router.post("/content/items/{content_id}/images/from-asset")
async def attach_asset_to_content(
    content_id: str,
    req: AttachReq,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
) -> dict[str, Any]:
    """把素材插进某篇内容的配图列表。

    为什么这样就能「不改发布链路」：`Content.images[].path` 的约定是
    **相对 UPLOAD_DIR 的路径**，而素材的 `storage_key` 正是
    `assets/<owner>/<yyyymm>/<id>.jpg` —— 同一个相对路径空间。
    所以 wechat.py 推草稿时的兜底分支 `Path(UPLOAD_DIR) / path` 直接就能找到文件，
    `_replace_src_by_filename` 也能靠文件名把签名直链换成微信自己的 url。
    """
    asset = await _get_asset(db, req.asset_id, owner)
    content = await db.get(Content, content_id)
    if not content or (content.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")

    storage = get_storage()
    url = storage.url(asset.storage_key, owner) if asset.storage_key else ""
    imgs = list(content.images or [])
    imgs.append(
        {
            "id": uuid.uuid4().hex[:12],
            "path": asset.storage_key,
            "url": url,
            "alt": asset.title or "",
            "source": "library",  # 与 ai / upload / cover 区分，界面上可显示来源徽标
            "prompt": "",
            "position": -1,
            "asset_id": asset.id,
        }
    )
    content.images = imgs
    content.updated_at = int(time.time())

    asset.used_count = (asset.used_count or 0) + 1
    asset.last_used_at = int(time.time())
    await db.commit()
    return {"ok": True, "images": imgs}


# ---------------------------------------------------------------- 内部工具
async def _get_asset(db: AsyncSession, asset_id: str, owner: str) -> Asset:
    """取素材并校验归属。不存在与不属于当前用户都当 404，不泄露存在性。"""
    asset = await db.get(Asset, asset_id)
    if not asset or asset.owner != owner:
        raise HTTPException(404, "素材不存在")
    return asset
