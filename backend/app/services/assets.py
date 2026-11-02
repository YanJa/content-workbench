"""素材库业务逻辑：入库、去重、缩略图、容量、引用。

放进 service 而不是 router，是因为「导入一张在线图」和「用户上传一张图」要走
完全相同的落盘与去重流程（差别只在元数据来源）。路由只负责收参数与鉴权。

去重的两道关，顺序有讲究：
    1. `(owner, source_name, source_id)` —— 在线素材在**下载前**就能判重，
       省一次网络请求和几十次磁盘写；
    2. `(owner, hash_sha256)` —— 落盘后判重，兜住「同一张图来自不同源」
       （Pixabay 和 Openverse 都收录了同一张 CC0 图的情况真实存在）。
"""
from __future__ import annotations

import hashlib
import io
import time
import uuid
from typing import Any

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.asset import Asset
from app.services.storage import get_storage

# 允许的图片后缀与对应 MIME。白名单而不是黑名单：PIL 能开的格式很多，
# 但能开不等于该收（比如 .psd、.tiff 存进去前端预览不了）。
_IMAGE_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_VIDEO_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}

# 缩略图长边。列表页一屏几十张，用原图会拖垮带宽；400px 足够卡片展示。
_THUMB_EDGE = 400


class AssetError(Exception):
    """素材操作的可预期失败（超限、格式不支持）。路由层转 400。"""


def _key(owner: str, month: str, asset_id: str, ext: str, *, thumb: bool = False) -> str:
    """构造存储 key：`assets/<owner>/<yyyymm>/<id>[_thumb].<ext>`。

    按 owner 分目录而不是按日期平铺：删除一个用户时能整目录清掉；
    按月份再分一层，单目录文件数不会无限增长（ext4 大目录会变慢）。
    """
    suffix = f"_thumb{ext}" if thumb else ext
    return f"assets/{owner}/{month}/{asset_id}{suffix}"


def _ext_of(filename: str, kind: str) -> tuple[str, str]:
    """从文件名推断 (后缀, MIME)。"""
    lower = (filename or "").lower()
    dot = lower.rfind(".")
    ext = lower[dot:] if dot >= 0 else ""
    table = _IMAGE_EXT if kind == "image" else _VIDEO_EXT
    if ext not in table:
        # 没后缀的图（粘贴上传常见）按 PNG 兜底，别直接拒 ——
        # 真正的格式校验交给 PIL，它开不了会抛异常，那才是硬判据。
        ext = ".png" if kind == "image" else ".mp4"
    return ext, table.get(ext, "application/octet-stream")


def _probe_image(data: bytes) -> tuple[int, int]:
    """读图片尺寸。顺带验证「这确实是一张图」——PIL 打不开就抛。"""
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def _make_thumb(data: bytes) -> tuple[bytes, str] | None:
    """生成缩略图，返回 (bytes, ext)。生成失败返回 None（不影响主流程）。

    统一输出 WebP：同样的观感下体积约为 JPEG 的 60-70%，而 Chrome/Safari/微信
    内置浏览器都支持。若 PIL 编译时没带 WebP 支持则回落 JPEG。
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB") if im.mode in ("P", "LA", "RGBA") else im
            im.thumbnail((_THUMB_EDGE, _THUMB_EDGE))
            buf = io.BytesIO()
            try:
                im.save(buf, format="WEBP", quality=80, method=4)
                return buf.getvalue(), ".webp"
            except (KeyError, OSError):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=82)
                return buf.getvalue(), ".jpg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成缩略图失败（保留原图，不影响入库）：%s", exc)
        return None


async def used_bytes(db: AsyncSession, owner: str) -> int:
    """当前已用容量（字节）。聚合查询，不需要额外维护计数表。"""
    total = (
        await db.execute(
            select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(Asset.owner == owner)
        )
    ).scalar()
    return int(total or 0)


async def _assert_room(db: AsyncSession, owner: str, incoming: int) -> None:
    quota = max(1, settings.ASSET_QUOTA_MB) * 1024 * 1024
    used = await used_bytes(db, owner)
    if used + incoming > quota:
        raise AssetError(
            f"素材库容量已满（已用 {used / 1048576:.0f}MB / 上限 {settings.ASSET_QUOTA_MB}MB），"
            "请先删掉一些素材再上传"
        )


async def find_by_source(db: AsyncSession, owner: str, source_name: str, source_id: str) -> Asset | None:
    """下载前判重：同一源站的同一个 ID 只导一次。"""
    if not source_name or not source_id:
        return None
    return (
        await db.execute(
            select(Asset).where(
                Asset.owner == owner,
                Asset.source_name == source_name,
                Asset.source_id == str(source_id),
            )
        )
    ).scalar_one_or_none()


async def find_by_hash(db: AsyncSession, owner: str, digest: str) -> Asset | None:
    return (
        await db.execute(select(Asset).where(Asset.owner == owner, Asset.hash_sha256 == digest))
    ).scalar_one_or_none()


async def save_bytes(
    db: AsyncSession,
    owner: str,
    data: bytes,
    *,
    kind: str = "image",
    source: str = "upload",
    source_name: str = "",
    source_id: str = "",
    source_url: str = "",
    license: str = "unknown",
    license_url: str = "",
    author: str = "",
    author_url: str = "",
    attribution_text: str = "",
    title: str = "",
    tags: list[str] | None = None,
    mime: str = "",
    filename: str = "",
) -> tuple[Asset, bool]:
    """落盘 + 入库。返回 (asset, created)。created=False 表示命中已有素材。

    调用方（上传 / 在线导入）共用这一条路径，避免两条链路的去重逻辑越长越不一样。
    """
    if not data:
        raise AssetError("文件内容为空")

    limit = max(1, settings.ASSET_MAX_MB) * 1024 * 1024
    if len(data) > limit:
        raise AssetError(f"文件超过 {settings.ASSET_MAX_MB}MB 上限")
    await _assert_room(db, owner, len(data))

    ext, guessed = _ext_of(filename, kind)
    mime = mime or guessed

    width = height = 0
    if kind == "image":
        try:
            width, height = _probe_image(data)
        except Exception as exc:  # noqa: BLE001
            raise AssetError("这不是一张能识别的图片（文件可能已损坏）") from exc

    digest = hashlib.sha256(data).hexdigest()
    dup = await find_by_hash(db, owner, digest)
    if dup:
        # 内容一致就直接复用已有那条：顺手把这次带来的更完整元数据补上
        # （典型场景：先手动上传、后从 Openverse 导入同一张，后者有署名信息）
        changed = False
        for field, value in (
            ("source_url", source_url),
            ("license", "" if license == "unknown" else license),
            ("attribution_text", attribution_text),
            ("author", author),
        ):
            if value and not getattr(dup, field):
                setattr(dup, field, value)
                changed = True
        if changed:
            await db.commit()
        return dup, False

    asset_id = uuid.uuid4().hex[:12]
    month = time.strftime("%Y%m")
    key = _key(owner, month, asset_id, ext)
    storage = get_storage()
    await storage.put(key, data, mime)

    thumb_key = ""
    if kind == "image":
        thumb = _make_thumb(data)
        if thumb:
            tdata, text = thumb
            thumb_key = _key(owner, month, asset_id, text, thumb=True)
            await storage.put(thumb_key, tdata, "image/webp" if text == ".webp" else "image/jpeg")

    now = int(time.time())
    asset = Asset(
        id=asset_id,
        owner=owner,
        kind=kind,
        source=source,
        source_name=source_name,
        source_id=str(source_id or ""),
        source_url=source_url,
        license=license,
        license_url=license_url,
        author=author,
        author_url=author_url,
        attribution_text=attribution_text,
        title=title or (filename or "未命名素材")[:300],
        tags=tags or [],
        width=width,
        height=height,
        size_bytes=len(data),
        mime=mime,
        hash_sha256=digest,
        storage=settings.STORAGE_BACKEND,
        storage_key=key,
        thumb_key=thumb_key,
        created_at=now,
        updated_at=now,
    )
    db.add(asset)
    try:
        await db.commit()
    except IntegrityError:
        # 并发下同一张图同时入库：唯一约束会挡下一条，回滚后取已有那条。
        # 比「先查后插」可靠 —— 后者在两次请求交错时两条都能过检查。
        await db.rollback()
        existing = await find_by_hash(db, owner, digest)
        if existing:
            return existing, False
        raise
    await db.refresh(asset)
    logger.info(
        "素材入库 owner=%s id=%s kind=%s source=%s size=%dKB",
        owner, asset.id, kind, source_name or source, len(data) // 1024,
    )
    return asset, True


async def delete_asset(db: AsyncSession, asset: Asset) -> None:
    """删记录 + 删文件。文件删除失败只记日志（记录消失才是用户要的结果）。"""
    storage = get_storage()
    for key in (asset.storage_key, asset.thumb_key, asset.original_key):
        if key:
            await storage.delete(key)
    await db.delete(asset)
    await db.commit()


async def count_recent_imports(db: AsyncSession, owner: str, window_seconds: int = 3600) -> int:
    """近一小时从在线源导入的数量。用于卡合规上限。

    直接数 assets 表，不额外维护计数器：导入是低频动作（一次几张），
    一次带索引的 count 足够快，省一张表也省一处可能不同步的状态。
    """
    since = int(time.time()) - window_seconds
    return int(
        (
            await db.execute(
                select(func.count(Asset.id)).where(
                    Asset.owner == owner,
                    Asset.source == "search",
                    Asset.created_at >= since,
                )
            )
        ).scalar()
        or 0
    )


def asset_dict(asset: Asset) -> dict[str, Any]:
    """序列化为前端结构。url / thumb_url 每次现签，不落库。"""
    storage = get_storage()
    owner = asset.owner or "default"
    return {
        "id": asset.id,
        "kind": asset.kind,
        "source": asset.source,
        "source_name": asset.source_name,
        "source_url": asset.source_url,
        "license": asset.license,
        "license_url": asset.license_url,
        "author": asset.author,
        "author_url": asset.author_url,
        "attribution_text": asset.attribution_text,
        "title": asset.title,
        "tags": asset.tags or [],
        "width": asset.width,
        "height": asset.height,
        "duration": asset.duration,
        "size_bytes": asset.size_bytes,
        "mime": asset.mime,
        "watermark_state": asset.watermark_state,
        "used_count": asset.used_count,
        "created_at": asset.created_at,
        "url": storage.url(asset.storage_key, owner) if asset.storage_key else "",
        # 没缩略图（老数据或生成失败）就让前端回落到原图，而不是显示破图
        "thumb_url": storage.url(asset.thumb_key, owner)
        if asset.thumb_key
        else (storage.url(asset.storage_key, owner) if asset.storage_key else ""),
    }
