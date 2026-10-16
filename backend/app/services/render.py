"""视频渲染任务编排。

职责边界
--------
`video_pipeline.py` 只负责「怎么渲染」，这里只负责「什么时候渲染、
怎么记住渲染到哪一步、失败了怎么告诉别人」。

为什么不能用 asyncio.create_task 裸跑
--------------------------------------
之前直接 create_task，任务状态放内存缓存。两条硬伤：
  1. 进程一重启（supervisord 崩溃自愈、代码更新都会触发），正在跑的渲染
     和它的任务记录一起蒸发，前端还在轮询一个已经不存在的 job_id。
  2. 并发没人管：连点几次提交，几个 ffmpeg 同时抢 CPU，全都变慢。

现在的做法：
  - 任务落 SQLite，重启不丢；启动时把「服务重启前还在跑」的孤儿任务
    统一标成 failed，不让它挂着假装在跑（断点续渲染不值得，几秒重渲染更快）。
  - 用 Semaphore 串行渲染，CPU 峰值可控。
  - 渲染进度实时回写数据库，前端轮询看到的永远是最新状态。
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.core.logging import logger
from app.db.session import SessionLocal
from app.models.renderjob import RenderJob
from app.services import video_pipeline

# 渲染是 CPU 密集活，串行跑 —— 两个任务并行不如一个接一个快
_render_lock = asyncio.Semaphore(1)

# 正在执行的任务句柄（便于将来加「取消」；目前主要用来排查）
_active: dict[str, asyncio.Task] = {}


# ------------------------------------------------------------------ 提交
async def submit_render_job(
    script: str,
    ratio: str,
    voice: str,
    subtitle: bool,
    owner: str,
    *,
    source: str = "text",
    title: str = "",
    visual_scenes: list[dict] | None = None,
) -> dict[str, Any]:
    """登记任务并立刻返回，渲染交给后台 worker。"""
    params = {
        "script": script,
        "ratio": ratio,
        "voice": voice,
        "subtitle": subtitle,
        "source": source,
    }
    if visual_scenes:
        params["visual_scenes"] = visual_scenes
    async with SessionLocal() as db:
        job = RenderJob(
            owner=owner,
            params=params,
            title=title or script.replace("\n", " ")[:30] or "未命名视频",
            source=source,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    task = asyncio.create_task(_run(job_id))
    _active[job_id] = task

    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "message": "渲染任务已提交，可用 job_id 查询进度",
        "poll": f"{settings.API_PREFIX}/video/jobs/{job_id}",
    }


# ------------------------------------------------------------------ 查询
def _out(job: RenderJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "output_path": job.output_path,
        "error": job.error,
        "title": job.title,
        "source": job.source,
        "ratio": (job.params or {}).get("ratio", ""),
        "voice": (job.params or {}).get("voice", ""),
        # 回显用：点击历史任务时前端把口播稿/每页结构填回编辑区
        "script": (job.params or {}).get("script", ""),
        "visual_scenes": (job.params or {}).get("visual_scenes"),
        "duration_sec": job.duration_sec,
        "width": job.width,
        "height": job.height,
        "size_bytes": job.size_bytes,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        # 成片可访问 URL：前端直接拿来 <video> 预览/下载。
        # 路由挂在 /video/outputs/ 下，不是 /outputs/ —— 早期版本拼错过。
        # owner 走查询参数：<video>/<a> 是浏览器直链，带不了请求头。
        "output_url": (
            f"{settings.API_PREFIX}/video/outputs/{job.output_path}"
            f"?owner={job.owner or 'default'}"
            if job.output_path
            else ""
        ),
    }


async def get_job(job_id: str, owner: str = "default") -> dict[str, Any] | None:
    async with SessionLocal() as db:
        job = await db.get(RenderJob, job_id)
        if not job or (job.owner or "default") != owner:
            return None
        return _out(job)


async def list_jobs(
    owner: str = "default",
    page: int = 1,
    page_size: int = 10,
    status: str | None = None,
    keyword: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按创建时间倒序分页取任务，返回 (当页任务, 总条数)。

    对外的分页语义统一是 page/pageSize（1 基页码），数据库层才换成
    offset/limit —— 两种概念混在调用方最容易把偏移算错。

    status 为空/all 表示不过滤；keyword 按标题模糊匹配（不区分大小写）。
    """
    page = max(page, 1)
    page_size = max(1, page_size)

    conds = [RenderJob.owner == owner]
    if status and status != "all":
        conds.append(RenderJob.status == status)
    if keyword and keyword.strip():
        # SQLite 的 LIKE 对 ASCII 本就不区分大小写，中文无影响
        conds.append(RenderJob.title.like(f"%{keyword.strip()}%"))

    async with SessionLocal() as db:
        total = await db.scalar(select(func.count()).select_from(RenderJob).where(*conds))
        rows = await db.execute(
            select(RenderJob)
            .where(*conds)
            .order_by(RenderJob.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return [_out(j) for j in rows.scalars().all()], int(total or 0)


async def retry_job(job_id: str, owner: str) -> dict[str, Any] | None:
    """重新渲染（沿用上次的参数）。失败的任务才允许重跑。"""
    async with SessionLocal() as db:
        job = await db.get(RenderJob, job_id)
        if not job or job.owner != owner:
            return None
        if job.status not in ("failed", "done"):
            return None
        job.status = "queued"
        job.progress = 0
        job.stage = "排队中"
        job.error = ""
        job.finished_at = None
        await db.commit()
    task = asyncio.create_task(_run(job_id))
    _active[job_id] = task
    return await get_job(job_id)


async def update_job(
    job_id: str,
    owner: str,
    *,
    title: str | None = None,
    script: str | None = None,
    visual_scenes: list[dict] | None = None,
    voice: str | None = None,
) -> dict[str, Any] | None:
    """改名 / 改口播稿 / 改每页结构 / 换音色（渲染中的任务不允许改）。"""
    async with SessionLocal() as db:
        job = await db.get(RenderJob, job_id)
        if not job or job.owner != owner:
            return None
        if job.status in ("running", "queued"):
            raise RuntimeError("任务正在渲染中，等它跑完再改")
        if title is not None and title.strip():
            job.title = title.strip()[:100]
        params = dict(job.params or {})
        if script is not None:
            params["script"] = script
        if visual_scenes:
            params["visual_scenes"] = visual_scenes
        if voice is not None and voice.strip():
            params["voice"] = voice.strip()
        job.params = params
        await db.commit()
        await db.refresh(job)
        return _out(job)


async def delete_job(job_id: str, owner: str) -> bool:
    async with SessionLocal() as db:
        job = await db.get(RenderJob, job_id)
        if not job or job.owner != owner:
            return False
        out = job.output_path
        await db.delete(job)
        await db.commit()
    # 成片文件留着的话，删掉的任务会留下"幽灵文件"占磁盘，一并清掉
    if out:
        _safe_remove(out)
    return True


def _safe_remove(path: str) -> None:
    """只删 OUTPUT_DIR 内的文件，防止传一个脏路径把系统文件擦了。"""
    full = (Path(settings.OUTPUT_DIR) / path).resolve()
    if str(full).startswith(str(Path(settings.OUTPUT_DIR).resolve())) and full.is_file():
        full.unlink(missing_ok=True)


# ------------------------------------------------------------------ 执行
async def _run(job_id: str) -> None:
    async with _render_lock:
        try:
            async with SessionLocal() as db:
                job = await db.get(RenderJob, job_id)
                if not job:
                    return
                params = job.params or {}
                job_owner = job.owner or "default"
                job.status = "running"
                job.stage = "准备中"
                await db.commit()

            # 进度回调可能来自 asyncio.to_thread 的子线程（复刻音色逐段合成就是），
            # 子线程里没有默认事件循环，get_event_loop() 会直接抛异常。
            # 用 call_soon_threadsafe 把写库任务调度回主循环，跨线程也安全。
            loop = asyncio.get_running_loop()
            # 去重：复刻音色逐段合成会回调上百次，进度没变就不必写库
            last: list[int] = [0]

            def progress(p: int, stage: str) -> None:
                if p <= last[0]:
                    return
                last[0] = p
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(_mark_progress(job_id, p, stage))
                )

            workdir = Path(settings.OUTPUT_DIR) / f"work_{job_id}"
            out_name = f"{job_id}.mp4"
            out_path = Path(settings.OUTPUT_DIR) / out_name

            result = await video_pipeline.render_video(
                script=params.get("script", ""),
                voice=params.get("voice", "zh-CN-XiaoxiaoNeural"),
                ratio=params.get("ratio", "9:16"),
                workdir=workdir,
                output_path=out_path,
                on_progress=progress,
                visual_scenes=params.get("visual_scenes"),
                # 口播稿当字幕：默认开，前端可关
                subtitle=params.get("subtitle", True),
                # 复刻音色按 owner 隔离，渲染时要带着归属去取
                owner=job_owner,
            )

            async with SessionLocal() as db:
                job = await db.get(RenderJob, job_id)
                if job:
                    job.status = "done"
                    job.progress = 100
                    job.stage = "完成"
                    job.output_path = out_name
                    job.duration_sec = result.duration_sec
                    job.width = result.width
                    job.height = result.height
                    job.size_bytes = result.size_bytes
                    job.finished_at = int(time.time())
                    await db.commit()
            logger.info("渲染完成: %s (%.1fs, %.0fKB)", job_id, result.duration_sec, result.size_bytes / 1024)

        except Exception as exc:  # noqa: BLE001
            logger.exception("渲染失败: %s", job_id)
            async with SessionLocal() as db:
                job = await db.get(RenderJob, job_id)
                if job:
                    job.status = "failed"
                    job.error = str(exc)[:800]
                    job.finished_at = int(time.time())
                    await db.commit()
        finally:
            _active.pop(job_id, None)
            # 中间产物（wav/card/clip）留着只白占磁盘，成片已在 OUTPUT_DIR
            _cleanup_workdir(Path(settings.OUTPUT_DIR) / f"work_{job_id}")


async def _mark_progress(job_id: str, p: int, stage: str) -> None:
    async with SessionLocal() as db:
        job = await db.get(RenderJob, job_id)
        if job and job.status == "running":
            job.progress = p
            job.stage = stage
            await db.commit()


def _cleanup_workdir(path: Path) -> None:
    """删工作目录。不做递归 unlink 是为了防止 path 被算错时误删目录树。"""
    if not path.is_dir():
        return
    for item in path.iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)
    try:
        path.rmdir()
    except OSError:
        pass


# ------------------------------------------------------------------ 启动恢复
#
# 实例互斥锁：一个「启动时把 running 任务标失败」的逻辑，只有在确认
# **没有别的实例还活着**时才能执行。
#
# 踩过的坑：用 `uvicorn --port 8899` 起第二个进程连同一个 SQLite 做验证，
# 它的 recover_orphans() 把主进程正在跑的任务直接标成了 failed —— 工作其实
# 还在继续，只是状态被别的进程改坏了。
#
# 用 flock 排他锁：第一个实例拿到锁并持有整个进程生命周期（进程退出时内核
# 自动释放），后来的实例拿不到锁就跳过孤儿清理。
_LOCK_FILE = Path(os.getenv("RENDER_LOCK_FILE", "/tmp/cwb-render.lock"))
_lock_fd: int | None = None


def _acquire_instance_lock() -> bool:
    """尝试取得「本进程是唯一实例」的锁。拿不到说明已有实例在跑。"""
    global _lock_fd
    import fcntl

    try:
        fd = os.open(_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # 打不开锁文件就别冒险清理，宁可漏掉孤儿任务
        logger.warning("无法打开实例锁文件 %s，跳过孤儿任务清理", _LOCK_FILE)
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    # 锁跟着 fd 活，必须保留 fd 不关闭，否则进程退出前锁就没了
    _lock_fd = fd
    return True


async def recover_orphans() -> int:
    """服务重启后，把之前「正在跑/排队中」的任务标成失败。

    不做断点续渲染：文案转视频全流程不过十几秒，重新跑一次比
    记住渲染到哪一帧简单得多。

    **只在确认没有其他实例存活时才做**（见模块内 _acquire_instance_lock 注释）：
    否则多开一个进程（哪怕只是拿别的端口做验证）就会把真正在跑的任务
    误标成失败。
    """
    if not _acquire_instance_lock():
        logger.info("已有实例持有渲染锁，跳过多余的孤儿任务清理")
        return 0

    async with SessionLocal() as db:
        rows = await db.execute(
            select(RenderJob).where(RenderJob.status.in_(["running", "queued"]))
        )
        jobs = rows.scalars().all()
        now = int(time.time())
        for j in jobs:
            j.status = "failed"
            j.error = "服务重启，渲染中断（点「重试」可重新渲染）"
            j.finished_at = now
        if jobs:
            await db.commit()
    if jobs:
        logger.warning("恢复 %d 个中断的渲染任务为失败", len(jobs))
    return len(jobs)
