"""内容加工路由：排版 + 配图。

对话创作产出纯内容，这里承接「把内容加工成可发布形态」的动作：
排版（公众号 HTML / 小红书文本）、配图（生成/上传/管理）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rbac import require_use
from app.core.security import link_url
from app.core.tenant import current_owner_id, link_auth
from app.core.logging import logger
from app.db.session import get_db
from app.models.content import Content
from app.models.user import User
from app.schemas.models import (
    CoverGenRequest,
    ImageGenRequest,
    ImageUpdateRequest,
    LayoutRequest,
)
from app.services import covers, imagegen
from app.services.layout import (
    cancel_layout_job_for_content,
    get_layout_job,
    list_themes,
    submit_layout_job,
)

router = APIRouter(prefix="/content", tags=["内容加工"])


async def _get_content(db: AsyncSession, content_id: str, owner: str = "default") -> Content:
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "内容不存在")
    return c


def _image_dir(content_id: str) -> Path:
    d = Path(settings.UPLOAD_DIR) / "gen" / content_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _image_url(content_id: str, filename: str, owner: str = "default") -> str:
    # 浏览器 <img src> 带不了请求头，用 HMAC 签名直链（见 core/security.link_url）
    return link_url(f"{settings.API_PREFIX}/content/images/{content_id}/{filename}", owner)


# ---------------------------------------------------------------- 排版
@router.get("/items/{content_id}/theme-options")
async def theme_options(content_id: str, owner: str = Depends(current_owner_id)):
    """可选排版主题（供前端选主题）+ 生图/封面能力可用性。

    返回的是全局静态配置，但路径带 content_id，所以要求登录 ——
    免得将来有人往函数体里加一句按 id 查库，就变成越权读。
    """
    return {
        "themes": list_themes(),
        "default": "moyu-green",
        "gen_ready": imagegen.configured(),
        "cover_ready": covers.available(),
        "cover_styles": covers.COVER_STYLES,
        "cover_sizes": covers.COVER_SIZES,
        "cover_default_style": settings.COVER_STYLE,
        "cover_default_tag": settings.COVER_TAG,
    }


@router.post("/items/{content_id}/layout")
async def do_layout(
    content_id: str,
    req: LayoutRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("layout.run", "layout")),
):
    """提交排版任务（**秒回**）。

    公众号排版走 pro 模型装配 + 校验回修，单次约 3 分钟，远超前端 60s 超时。
    所以这里只登记任务并返回 task_id，前端轮询
    `GET /content/items/{id}/layout/{task_id}` 拿进度与结果。
    """
    c = await _get_content(db, content_id, owner)  # 校验存在 + 归属
    # 幂等保护：已在排版中就直接拒绝，避免重复提交烧 token
    if (c.processing or "") == "layout":
        return {"ok": False, "message": "该内容正在排版中，请稍候或点「取消」"}
    return await submit_layout_job(
        c.id,
        theme_id=req.theme,
        platform=req.platform,
        auto_cover=req.auto_cover,
        author_name=req.author_name,
        author_intro=req.author_intro,
        account_id=req.account_id,
        auto_tags=req.auto_tags,
    )


@router.get("/items/{content_id}/layout/{task_id}")
async def layout_job_status(
    content_id: str,
    task_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """轮询排版任务进度 / 结果。

    404 表示任务已中断（进程重启后内存任务表清空），前端据此提示「重新排版」。
    """
    # 归属校验先做：内容不属于该用户时当作「任务不存在」，不泄露存在性
    await _get_content(db, content_id, owner)
    job = get_layout_job(task_id)
    if job is None or job["content_id"] != content_id:
        raise HTTPException(404, "排版任务不存在或已中断，请重新排版")
    return job


@router.post("/items/{content_id}/cancel-processing")
async def cancel_processing(
    content_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """取消某内容的「加工中」状态（解卡）。

    用途：排版/生图任务因进程重启、异常或用户中途关面板而卡住时，
    `Content.processing` 会残留在库里，导致队列页该行所有按钮被禁用。
    这个端点解除该标记，并把内存里仍在跑的任务标记成「已取消」。

    说明：排版协程在串行锁里跑，无法从外部真正杀掉；这里保证的是
    「标记解除 + 前端停止等待」，让用户能重新操作。
    """
    c = await _get_content(db, content_id, owner)
    cleared = c.processing or ""
    if cleared:
        c.processing = ""
        await db.commit()
    task_id = cancel_layout_job_for_content(content_id)
    logger.info("取消加工 content=%s 原标记=%s task=%s", content_id, cleared, task_id)
    return {"ok": True, "cleared": cleared, "task_id": task_id}


# ---------------------------------------------------------------- 配图
@router.post("/items/{content_id}/images/generate")
async def generate_images(
    content_id: str,
    req: ImageGenRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    # 生图只受权限管控，不计次（用户明确没把它纳入配额）
    _perm: User = Depends(require_use("layout.image_gen")),
):
    """调生图通道生成配图，追加到内容里。provider=script(本地脚本)/cloudflare(Cloudflare AI)。"""
    c = await _get_content(db, content_id, owner)
    provider = req.provider or "script"
    if provider == "cloudflare" and not imagegen.cloudflare_configured():
        raise HTTPException(
            400, "未配置 Cloudflare（CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID）。"
        )
    if provider == "script" and not imagegen.script_configured():
        raise HTTPException(
            400,
            "未配置生图脚本（IMAGE_SCRIPT）。可在 .env 中配置，或改用手动上传图片。",
        )
    out_dir = _image_dir(content_id)
    # 标记「生图中」：前端轮询队列/内容时能显示状态、避免重复点
    c.processing = "image"
    await db.commit()
    try:
        files = await imagegen.generate_image(
            prompt=req.prompt, size=req.size, n=req.count, out_dir=out_dir, provider=provider
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("生图失败 content=%s", content_id)
        raise HTTPException(500, f"生图失败：{exc}") from exc
    finally:
        # 无论成败都清标记，否则会一直显示「生图中」
        c.processing = ""
        await db.commit()

    imgs = list(c.images or [])
    for f in files:
        imgs.append(
            {
                "id": uuid.uuid4().hex[:12],
                "path": str(f.relative_to(Path(settings.UPLOAD_DIR))),
                "url": _image_url(content_id, f.name, owner),
                "alt": "",
                "source": "gen",
                "prompt": req.prompt,
                "position": req.position,
            }
        )
    c.images = imgs
    await db.commit()
    await db.refresh(c)
    return {"ok": True, "images": c.images}


@router.post("/items/{content_id}/images/upload")
async def upload_image(
    content_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """手动上传配图（无生图脚本时的兜底路径）。"""
    c = await _get_content(db, content_id, owner)
    suffix = Path(file.filename or "").suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(400, "仅支持 png/jpg/webp/gif 图片")
    out_dir = _image_dir(content_id)
    fname = f"{uuid.uuid4().hex[:12]}{suffix}"
    dest = out_dir / fname
    dest.write_bytes(await file.read())

    imgs = list(c.images or [])
    imgs.append(
        {
            "id": uuid.uuid4().hex[:12],
            "path": str(dest.relative_to(Path(settings.UPLOAD_DIR))),
            "url": _image_url(content_id, fname, owner),
            "alt": "",
            "source": "upload",
            "prompt": "",
            "position": -1,
        }
    )
    c.images = imgs
    await db.commit()
    await db.refresh(c)
    return {"ok": True, "images": c.images}


@router.put("/items/{content_id}/images/{image_id}")
async def update_image(
    content_id: str,
    image_id: str,
    req: ImageUpdateRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """改配图说明 / 排序位置。"""
    c = await _get_content(db, content_id, owner)
    imgs = list(c.images or [])
    for img in imgs:
        if img.get("id") == image_id:
            if req.alt is not None:
                img["alt"] = req.alt
            if req.position is not None:
                img["position"] = req.position
            break
    else:
        raise HTTPException(404, "配图不存在")
    c.images = imgs
    await db.commit()
    await db.refresh(c)
    return {"ok": True, "images": c.images}


@router.delete("/items/{content_id}/images/{image_id}")
async def delete_image(
    content_id: str,
    image_id: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """删除配图（连带删文件）。"""
    c = await _get_content(db, content_id, owner)
    imgs = list(c.images or [])
    keep, removed = [], None
    for img in imgs:
        if img.get("id") == image_id:
            removed = img
        else:
            keep.append(img)
    if removed is None:
        raise HTTPException(404, "配图不存在")
    # 删文件（path 相对 UPLOAD_DIR）
    try:
        f = Path(settings.UPLOAD_DIR) / removed["path"]
        if f.exists():
            f.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除配图文件失败（忽略）: %s", exc)
    c.images = keep
    await db.commit()
    await db.refresh(c)
    return {"ok": True, "images": c.images}


# ---------------------------------------------------------------- 封面
@router.post("/items/{content_id}/covers/generate")
async def generate_cover(
    content_id: str,
    req: CoverGenRequest,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("layout.cover")),
):
    """生成封面并**插到配图首位**（index 1），使 `<!--IMG:1-->` 指向封面。

    封面是模板化产物（HTML + 无头浏览器截图），文字确定、品牌统一；
    与 AI 文内配图（`/images/generate`）互不影响。不满意可再调一次覆盖，
    也可以在配图区删除/上传替换。
    """
    c = await _get_content(db, content_id, owner)
    if not covers.available():
        raise HTTPException(
            400,
            "封面通道不可用：请确认 COVER_SCRIPT 指向的脚本存在，且本机装有 Chrome/Chromium。",
        )

    title, sub = req.title.strip(), req.sub.strip()
    if not title:
        # 没给标题就从正文解析（前端通常已预填，这里是兜底）
        title, auto_sub = covers.derive_cover_text(c.body or "")
        sub = sub or auto_sub
    if not title:
        raise HTTPException(400, "正文为空，无法自动生成封面标题，请手动填写")

    out_dir = _image_dir(content_id)
    c.processing = "image"
    await db.commit()
    try:
        path = await covers.generate_cover(
            title,
            out_dir,
            sub=sub,
            tag=req.tag,
            style=req.style,
            size=req.size,
            date=req.date,
            theme=req.theme,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成封面失败 content=%s", content_id)
        raise HTTPException(500, f"生成封面失败：{exc}") from exc
    finally:
        c.processing = ""
        await db.commit()

    # 已有封面 → 换掉（保留它在队首的位置）；没有 → 插到首位
    imgs = list(c.images or [])
    new_img = {
        "id": uuid.uuid4().hex[:12],
        "path": str(path.relative_to(Path(settings.UPLOAD_DIR))),
        "url": _image_url(content_id, path.name, owner),
        "alt": "封面",
        "source": "cover",
        "prompt": f"{title} / {sub}".strip(" /"),
        "position": 0,
    }
    cover_idx = next(
        (i for i, im in enumerate(imgs) if im.get("source") == "cover"), None
    )
    if cover_idx is None:
        imgs.insert(0, new_img)
        # 旧封面占的文件清掉（换图场景在下面处理）
    else:
        old = imgs[cover_idx]
        imgs[cover_idx] = new_img
        try:  # 删旧封面文件，避免堆积
            of = Path(settings.UPLOAD_DIR) / (old.get("path") or "")
            if old.get("path") and of.exists():
                of.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除旧封面文件失败（忽略）: %s", exc)

    c.images = imgs
    await db.commit()
    await db.refresh(c)
    return {"ok": True, "images": c.images}


# ---------------------------------------------------------------- 图片静态服务
@router.get("/image-providers")
async def image_providers(owner: str = Depends(current_owner_id)) -> dict[str, bool]:
    """文内配图可用通道能力位（前端据此决定显示哪些生成方式）。

    不读 owner，但挂守卫依赖：能力探测接口同样不该对未登录流量敞开
    （会泄露部署配置，test_every_api_endpoint_is_guarded 也要求全端点有守卫）。
    """
    return {
        "script": imagegen.script_configured(),
        "cloudflare": imagegen.cloudflare_configured(),
    }


@router.get("/images/{content_id}/{filename}")
async def serve_image(
    content_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(link_auth),
):
    """提供配图文件（预览 / 公众号排版预览用）。

    <img src> 是浏览器直链、带不了请求头，用 HMAC 签名 URL
    （?o=<uid>&sig=...，生成见 core/security.link_url）；同源前端
    fetch 也可以带 Bearer 头。
    """
    # 防路径穿越：只允许这两个段是纯文件名
    if "/" in content_id or "/" in filename or ".." in filename:
        raise HTTPException(400, "非法路径")
    # 归属校验：内容不存在或不属于该 owner，都当作「图片不存在」
    c = await db.get(Content, content_id)
    if not c or (c.owner or "default") != owner:
        raise HTTPException(404, "图片不存在")
    f = Path(settings.UPLOAD_DIR) / "gen" / content_id / filename
    if not f.exists() or not f.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(f)
