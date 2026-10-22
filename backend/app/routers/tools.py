"""工具与视频路由。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rbac import require_use
from app.core.tenant import current_owner_id, link_auth
from app.db.session import get_db
from app.models.slide import PptSlideClaim
from app.models.user import User
from app.schemas.models import (
    JobOut,
    JobPage,
    JobPatch,
    JobProgress,
    ToolOut,
    VideoRenderRequest,
)
from app.services.ppt_parser import PPTParseError, pptx_to_script
from app.services.render import (
    delete_job,
    get_job,
    list_jobs,
    retry_job,
    submit_render_job,
    update_job,
)
from app.tools.registry import registry

router = APIRouter(prefix="/tools", tags=["工具"])


@router.get("", response_model=list[ToolOut])
async def list_tools():
    """列出所有可用工具及其是否需要人工确认。

    刻意走 `registry.schemas()` 而不是遍历 `registry.all()` 逐字段读属性：
    静态 `Tool` 和动态 `DynamicTool` 是两种 dataclass，逐字段读迟早会在某一种
    上 AttributeError（2026-10-22 就是这么 500 的，前端 catch 成空数组，整张
    卡片显示「可用工具（0）」）。`schemas()` 是注册表自己保证出口形状的地方，
    展示和发给模型用的是同一份定义。
    """
    need_confirm = {
        s["function"]["name"]: registry.needs_confirm(s["function"]["name"])
        for s in registry.schemas()
    }
    return [
        ToolOut(
            name=t["function"]["name"],
            description=t["function"]["description"],
            requires_confirm=need_confirm.get(t["function"]["name"], False),
        )
        for t in registry.schemas()
    ]


video_router = APIRouter(prefix="/video", tags=["视频"])


def _job_out(job: dict) -> JobOut:
    """直接整包喂给 JobOut，新增字段不用每次手工抄一遍。"""
    return JobOut(**{k: v for k, v in job.items() if k in JobOut.model_fields})


@video_router.post("/render")
async def render(
    req: VideoRenderRequest,
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("video.render", "video")),
):
    """提交视频渲染任务（异步）。"""
    if not req.script.strip():
        raise HTTPException(400, "口播稿不能为空")
    # 音色在提交时就校验，别等渲染跑到配音阶段才失败
    if req.voice:
        from app.services.video_pipeline import valid_voices
        from app.services.voices import get_profile, is_clone_voice

        if is_clone_voice(req.voice):
            # 复刻音色：不在 edge-tts 清单里，单独校验音色库是否有这条
            if not get_profile(req.voice[len("clone_"):]):
                raise HTTPException(400, "该复刻音色不存在或已删除，请重新选择配音音色")
        elif req.voice not in await valid_voices():
            raise HTTPException(400, f"音色 {req.voice} 当前不可用，请换一个配音音色")
    job = await submit_render_job(
        script=req.script,
        ratio=req.ratio,
        voice=req.voice,
        subtitle=req.subtitle,
        owner=owner,
        source="ppt" if req.visual_scenes else "text",
        title=req.title,
        visual_scenes=[v.model_dump() for v in req.visual_scenes] if req.visual_scenes else None,
    )
    return job


@video_router.get("/jobs", response_model=JobPage)
async def job_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100, alias="pageSize"),
    status: str | None = Query(None, description="按状态过滤：queued/running/done/failed"),
    keyword: str | None = Query(None, description="按标题模糊搜索"),
    owner: str = Depends(current_owner_id),
):
    """任务列表（分页 + 状态过滤 + 标题搜索，按创建时间倒序）。"""
    items, total = await list_jobs(
        owner, page=page, page_size=page_size, status=status, keyword=keyword
    )
    return {
        "items": [_job_out(j) for j in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    }


@video_router.get("/jobs/{job_id}/progress", response_model=JobProgress)
async def job_progress(job_id: str, owner: str = Depends(current_owner_id)):
    """轮询专用的轻量进度接口（约 200 字节，完整接口约 22KB）。

    轮询只要「跑到哪了」，不需要 script / visual_scenes 那两个大字段 ——
    后者每 2 秒跨境传十几 KB，经 Cloudflare Tunnel 单次要 2 秒左右。
    任务结束时前端再取一次完整详情回填编辑区。
    """
    job = await get_job(job_id, owner)
    if not job:
        raise HTTPException(404, "任务不存在")
    return JobProgress(**{k: v for k, v in job.items() if k in JobProgress.model_fields})


@video_router.get("/jobs/{job_id}", response_model=JobOut)
async def job_status(job_id: str, owner: str = Depends(current_owner_id)):
    """查询渲染进度。"""
    job = await get_job(job_id, owner)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _job_out(job)


@video_router.patch("/jobs/{job_id}", response_model=JobOut)
async def job_update(job_id: str, patch: JobPatch, owner: str = Depends(current_owner_id)):
    """改名 / 改口播稿 / 改每页结构 / 换音色（渲染中的任务返回 409）。"""
    try:
        job = await update_job(
            job_id,
            owner,
            title=patch.title,
            script=patch.script,
            visual_scenes=[v.model_dump() for v in patch.visual_scenes]
            if patch.visual_scenes
            else None,
            voice=patch.voice,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not job:
        raise HTTPException(404, "任务不存在")
    return _job_out(job)


@video_router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def job_retry(
    job_id: str,
    owner: str = Depends(current_owner_id),
    # retry 必须也记账：漏了它就能靠反复重跑无限消耗渲染资源
    _perm: User = Depends(require_use("video.render", "video")),
):
    """失败的任务沿原参数重跑。"""
    job = await retry_job(job_id, owner)
    if not job:
        raise HTTPException(400, "任务不存在，或当前状态不可重跑")
    return _job_out(job)


@video_router.delete("/jobs/{job_id}")
async def job_delete(job_id: str, owner: str = Depends(current_owner_id)):
    """删除任务，连带清掉成片文件。"""
    if not await delete_job(job_id, owner):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@video_router.post("/ppt/parse")
async def ppt_parse(
    file: UploadFile = File(...),
    owner: str = Depends(current_owner_id),
    _perm: User = Depends(require_use("video.ppt_parse")),
):
    """上传 pptx，解析出可朗读的口播稿（文字 + 备注），不渲染。

    前端拿到文本后让用户确认/改一改，再调 /render 走同一管线 —
    否则生成什么内容对用户是黑盒。

    必须登录：不鉴权的话任何人都能往磁盘写幻灯片截图目录。
    """
    if not (file.filename or "").lower().endswith((".pptx", ".ppt")):
        raise HTTPException(400, "请上传 .pptx 或 .ppt 文件")
    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"文件超过 {settings.MAX_UPLOAD_MB}MB")
    try:
        result = pptx_to_script(content, file.filename or "ppt")
    except PPTParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    # 把每页渲染成真实幻灯片图（LibreOffice，CPU 密集，丢线程池）。
    # 渲染不出来就退回文字卡片，解析本身不受影响。
    import asyncio

    from app.services.ppt_parser import render_slide_images

    images = await asyncio.to_thread(render_slide_images, content, len(result["visual_scenes"]))
    for scene, img in zip(result["visual_scenes"], images):
        scene["slide_image"] = img
    result["slide_images_ok"] = sum(1 for x in images if x)
    return result


# ---------------------------------------------------------------- 成片访问
@video_router.get("/voices", tags=["视频"])
async def voices():
    """edge-tts 当前可用的全部音色（前端选择器可以动态拉取）。"""
    from app.services.video_pipeline import valid_voices

    return {"voices": await valid_voices()}


@video_router.get("/preview-card", tags=["视频"])
async def preview_card(
    title: str = "", body: str = "", idx: int = 0, total: int = 1, slide_image: str = "",
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(link_auth),
):
    """单页「画面卡片」实时预览：PPT 编辑区每页旁边的缩略图就是它。

    和渲染管线用同一个 build_card，所见即所得；参数走 query 是为了能直接
    当 <img> 的 src。传了 slide_image（真实幻灯片图）就按真图合成 ——
    读取前先过 slides 目录归属认领（见 _claim_slide_folder）。
    """
    import tempfile

    from app.services.video_pipeline import Scene, build_card, _slide_photo_card

    if slide_image:
        path = _resolve_slide_image(slide_image)
        if path is not None:
            await _claim_slide_folder(db, slide_image, owner)
            out = Path(tempfile.mkdtemp(prefix="cwb_card_")) / "card.png"
            _slide_photo_card(path, (720, 1280), out)
            return FileResponse(out, media_type="image/png")

    scene = Scene(
        text=title,
        start=0.0,
        end=1.0,
        visual_title=title,
        visual_body=tuple(x.strip() for x in body.split("\n") if x.strip()),
    )
    out = Path(tempfile.mkdtemp(prefix="cwb_card_")) / "card.png"
    build_card(scene, max(idx, 0), max(total, 1), (720, 1280), out)
    return FileResponse(out, media_type="image/png")


def _resolve_slide_image(rel: str) -> Path | None:
    """把 slides/<folder>/page_N.png 解析成绝对路径，格式不符直接拒绝。"""
    import re

    m = re.fullmatch(r"slides/([A-Za-z0-9_-]+)/(page_\d+\.png)", rel)
    if not m:
        return None
    path = Path(settings.OUTPUT_DIR) / "slides" / m.group(1) / m.group(2)
    return path if path.is_file() else None


async def _claim_slide_folder(db: AsyncSession, slide_image: str, owner: str) -> None:
    """slides/<folder>/ 目录的归属懒认领。

    folder = md5(pptx 内容)[:16]，两个人上传同一份 PPT 会撞目录：
    谁先把渲染产物写进去，另一个人（或任何拿到路径的人）不鉴权就能读。
    首次访问登记归属；之后只有归属者（或持签名直链者）能读，
    归属不符 404 —— 不泄露「这个目录存在」。
    """
    import re

    m = re.fullmatch(r"slides/([A-Za-z0-9_-]+)/(page_\d+\.png)", slide_image or "")
    if not m or len(m.group(1)) > 16:  # 列宽防御：slide_key 是 String(16)
        raise HTTPException(404, "图片不存在")
    folder = m.group(1)
    claim = (
        await db.execute(select(PptSlideClaim).where(PptSlideClaim.slide_key == folder))
    ).scalar_one_or_none()
    if claim is None:
        db.add(PptSlideClaim(slide_key=folder, owner=owner))
        await db.commit()
    elif claim.owner != owner:
        raise HTTPException(404, "图片不存在")


@video_router.get("/slide-images/{folder}/{filename}", tags=["视频"])
async def serve_slide_image(
    folder: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(link_auth),
):
    """PPT 每页的真实幻灯片图（LibreOffice 渲染产物）。

    slides 目录按内容哈希命名、跨用户会撞车，读取前过归属认领
    （见 _claim_slide_folder）。
    """
    path = _resolve_slide_image(f"slides/{folder}/{filename}")
    if path is None:
        raise HTTPException(404, "图片不存在")
    await _claim_slide_folder(db, f"slides/{folder}/{filename}", owner)
    return FileResponse(path, media_type="image/png")


@video_router.get("/outputs/{filename}", tags=["视频"])
async def serve_output(filename: str, owner: str = Depends(link_auth)):
    """成片文件预览/下载。

    文件名只允许字母数字下划线中横线，防止 ../ 路径穿越 ——
    这里用的是前端传回来的名字，不能信。

    浏览器直链（<video>/<a> 的 src）用 HMAC 签名 URL（?o=&sig=，
    见 core/security.link_url）；同源前端 fetch 也可以带 Bearer 头。

    文件名就是 `<job_id>.mp4`（见 render._run），顺手按 job 归属校验，
    避免拿到别人的成片直链就能看。
    """
    if not _is_safe_name(filename):
        raise HTTPException(400, "非法文件名")
    job_id = Path(filename).stem
    job = await get_job(job_id, owner)
    if not job:
        raise HTTPException(404, "文件不存在（可能已被清理）")
    path = Path(settings.OUTPUT_DIR) / filename
    if not path.is_file():
        raise HTTPException(404, "文件不存在（可能已被清理）")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=filename,
        headers={"Accept-Ranges": "bytes"},
    )


def _is_safe_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "-_." for c in name) and ".." not in name
