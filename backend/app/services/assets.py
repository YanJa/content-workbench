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
import zipfile
from typing import Any

from PIL import Image
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.models.asset import Asset, AssetFolder
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
    folder_id: str | None = None,
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
        # 去重命中且目标条还没归类、本次又明确指定了目录 → 顺手归位。
        # 只补空不覆盖：重复上传同一张图不应把已整理好的素材拖走。
        if folder_id and not dup.folder_id:
            dup.folder_id = folder_id
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
        folder_id=folder_id,
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
        "folder_id": asset.folder_id or "",  # 空串 = 未归类
        "url": storage.url(asset.storage_key, owner) if asset.storage_key else "",
        # 没缩略图（老数据或生成失败）就让前端回落到原图，而不是显示破图
        "thumb_url": storage.url(asset.thumb_key, owner)
        if asset.thumb_key
        else (storage.url(asset.storage_key, owner) if asset.storage_key else ""),
    }


# ---------------------------------------------------------------- 目录（folder）
def folder_dict(f: AssetFolder) -> dict[str, Any]:
    return {
        "id": f.id,
        "name": f.name,
        "parent_id": f.parent_id or "",
        "created_at": f.created_at,
    }


async def list_folders(db: AsyncSession, owner: str) -> list[AssetFolder]:
    """用户全部目录（扁平）。前端组树，后端不做过滤/排序逻辑。"""
    rows = (
        (
            await db.execute(
                select(AssetFolder)
                .where(AssetFolder.owner == owner)
                .order_by(AssetFolder.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_folder(db: AsyncSession, folder_id: str, owner: str) -> AssetFolder:
    """取目录并校验归属。不存在与不属于当前用户都当 404。"""
    f = await db.get(AssetFolder, folder_id)
    if not f or f.owner != owner:
        raise AssetError("目录不存在")
    return f


async def create_folder(
    db: AsyncSession, owner: str, name: str, parent_id: str | None
) -> AssetFolder:
    name = (name or "").strip()[:120]
    if not name:
        raise AssetError("目录名不能为空")
    if parent_id:
        await get_folder(db, parent_id, owner)  # 校验父目录存在且属于当前用户
    # 同级重名先查一遍给友好报错（唯一约束挡并发，查询给文案）
    dup = (
        await db.execute(
            select(AssetFolder).where(
                AssetFolder.owner == owner,
                AssetFolder.parent_id == parent_id if parent_id else AssetFolder.parent_id.is_(None),
                AssetFolder.name == name,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise AssetError("同级已存在同名目录")
    f = AssetFolder(id=uuid.uuid4().hex[:12], owner=owner, name=name, parent_id=parent_id)
    db.add(f)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AssetError("同级已存在同名目录") from exc
    await db.refresh(f)
    return f


def _is_descendant(folders: list[AssetFolder], cand_id: str, ancestor_id: str) -> bool:
    """cand_id 是否是 ancestor_id 的后代（含间接）。"""
    parent_map = {f.id: f.parent_id for f in folders}
    cur = cand_id
    seen = set()
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        cur = parent_map.get(cur) or None
    return False


async def update_folder(
    db: AsyncSession,
    owner: str,
    folder: AssetFolder,
    *,
    name: str | None = None,
    parent_id: str | None = None,
    parent_set: bool = False,
) -> AssetFolder:
    """改名 / 移动。移动要防环：新父不能是自己或自己的后代。"""
    if name is not None:
        new_name = name.strip()[:120]
        if not new_name:
            raise AssetError("目录名不能为空")
        if new_name != folder.name:
            dup = (
                await db.execute(
                    select(AssetFolder).where(
                        AssetFolder.owner == owner,
                        AssetFolder.parent_id == folder.parent_id
                        if folder.parent_id
                        else AssetFolder.parent_id.is_(None),
                        AssetFolder.name == new_name,
                        AssetFolder.id != folder.id,
                    )
                )
            ).scalar_one_or_none()
            if dup:
                raise AssetError("同级已存在同名目录")
            folder.name = new_name
    if parent_set:
        new_parent = parent_id or None
        if new_parent == folder.id:
            raise AssetError("不能把目录移到它自己下面")
        if new_parent:
            await get_folder(db, new_parent, owner)
            all_folders = await list_folders(db, owner)
            if _is_descendant(all_folders, new_parent, folder.id):
                raise AssetError("不能把目录移到它自己的子目录下面")
        folder.parent_id = new_parent
    await db.commit()
    await db.refresh(folder)
    return folder


async def collect_subtree_ids(db: AsyncSession, owner: str, root_id: str) -> list[str]:
    """root_id 及其全部后代目录的 id 列表（含 root）。"""
    folders = await list_folders(db, owner)
    children: dict[str | None, list[str]] = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f.id)
    out: list[str] = []
    stack = [root_id]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


async def delete_folder(
    db: AsyncSession, owner: str, folder: AssetFolder, strategy: str
) -> dict[str, Any]:
    """删目录。strategy 决定目录内素材的去向：

    - move-to-parent：素材与子目录挂回被删目录的父级（子目录逐层上提）。
      简化实现：子目录的 parent_id 直接改到被删目录的 parent_id；
      素材 folder_id 同理。不保持"相对层级"，但对用户是无损的。
    - delete-assets：子目录连同其中素材**全部删除**（记录+文件）。
    """
    if strategy not in ("move-to-parent", "delete-assets"):
        raise AssetError("未知的删除策略")
    subtree = await collect_subtree_ids(db, owner, folder.id)
    if strategy == "move-to-parent":
        # 素材挂回父级；子目录也挂回父级（被删目录消失后层级压缩一层）
        await db.execute(
            Asset.__table__.update()
            .where(Asset.owner == owner, Asset.folder_id.in_(subtree))
            .values(folder_id=folder.parent_id)
        )
        await db.execute(
            AssetFolder.__table__.update()
            .where(
                AssetFolder.owner == owner,
                AssetFolder.id.in_(subtree),
                AssetFolder.id != folder.id,
            )
            .values(parent_id=folder.parent_id)
        )
        # 孙目录以下保持与"上提一层"的父目录的相对关系 —— 上面的 update
        # 把所有后代都挂到 folder.parent_id，这会让深层结构塌陷到一层。
        # 见 plan R1：接受塌陷（目录量小、用户可再整理），换取实现简单可靠。
        await db.delete(folder)
        await db.commit()
        return {"ok": True, "deleted_assets": 0, "moved_assets": True}
    # delete-assets：先删素材文件与记录，再删目录
    deleted_assets = 0
    for chunk_start in range(0, len(subtree), 50):
        chunk = subtree[chunk_start : chunk_start + 50]
        rows = (
            (
                await db.execute(
                    select(Asset).where(Asset.owner == owner, Asset.folder_id.in_(chunk))
                )
            )
            .scalars()
            .all()
        )
        for a in rows:
            await delete_asset(db, a)
            deleted_assets += 1
    await db.execute(
        AssetFolder.__table__.delete().where(
            AssetFolder.owner == owner, AssetFolder.id.in_(subtree)
        )
    )
    await db.commit()
    return {"ok": True, "deleted_assets": deleted_assets, "moved_assets": False}


async def descendant_folder_ids(db: AsyncSession, owner: str, root_id: str) -> set[str]:
    """某个目录及其所有子孙目录的 id 集合。

    目录树角标（folder_asset_counts）显示的是「含子目录」的递归计数，
    但 /assets 的 folder_id 过滤以前只匹配直属目录 —— 于是父目录角标
    显示 12、点进去却只看到 3 张直属图，看起来像筛选坏了。这里算出
    完整子树 id，让过滤口径与角标一致：点哪个目录就看它（含嵌套）全部图。
    """
    folders = await list_folders(db, owner)
    children: dict[str | None, list[str]] = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f.id)

    out: set[str] = set()

    def _walk(fid: str) -> None:
        out.add(fid)
        for c in children.get(fid, []):
            _walk(c)

    _walk(root_id)
    return out


async def folder_asset_counts(db: AsyncSession, owner: str) -> dict[str, int]:
    """每个目录（含间接）下的素材数。树形视图角标用。"""
    folders = await list_folders(db, owner)
    direct = dict(
        (
            await db.execute(
                select(Asset.folder_id, func.count(Asset.id))
                .where(Asset.owner == owner)
                .group_by(Asset.folder_id)
            )
        ).all()
    )
    children: dict[str | None, list[str]] = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f.id)

    counts: dict[str, int] = {}

    def _count(fid: str) -> int:
        n = int(direct.get(fid) or 0)
        for c in children.get(fid, []):
            n += _count(c)
        counts[fid] = n
        return n

    for f in folders:
        if not f.parent_id:
            _count(f.id)
    counts[""] = sum(int(v or 0) for k, v in direct.items() if not k)  # 未归类
    return counts


# ---------------------------------------------------------------- zip 打包
def _zip_entry_name(title: str, storage_key: str, used: set[str]) -> str:
    """zip 内文件名：优先 title（带扩展名），冲突加 (1)(2)…序号。"""
    import os
    import re

    ext = os.path.splitext(storage_key or "")[1] or ".bin"
    base = (title or os.path.basename(storage_key or "asset")).strip()
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base)[:80] or "asset"
    if not base.lower().endswith(ext.lower()):
        base = f"{base}{ext}"
    name = base
    i = 1
    while name in used:
        name = f"{base[: -len(ext)]}({i}){ext}"
        i += 1
    used.add(name)
    return name


async def build_assets_zip(db: AsyncSession, owner: str, ids: list[str]) -> tuple[bytes, int]:
    """把若干素材打包成 zip 字节流。返回 (zip_bytes, packed_count)。

    走 storage 抽象取字节（TOS 模式回源拉对象），不直接读本地盘 ——
    否则切到 s3 后端后本地没有文件，下载全是空 zip。
    """
    storage = get_storage()
    used: set[str] = set()
    buf = io.BytesIO()
    packed = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for aid in ids[:200]:
            asset = await db.get(Asset, aid)
            if not asset or asset.owner != owner or not asset.storage_key:
                continue
            try:
                data = await storage.get(asset.storage_key)
            except (ValueError, FileNotFoundError, OSError):
                logger.warning("zip 打包跳过（取不到文件）id=%s key=%s", aid, asset.storage_key)
                continue
            zf.writestr(_zip_entry_name(asset.title, asset.storage_key, used), data)
            packed += 1
    return buf.getvalue(), packed
