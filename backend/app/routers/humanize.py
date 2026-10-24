"""人工痕迹体检与「加痕迹」改写。

三个端点，两条成本完全不同的路径：

  - `POST /scan`：**零 LLM**。纯规则 + 统计，毫秒级、零成本。可以随便点。
  - `POST /rewrite`：**走 LLM**（reasoning 档），异步任务 + 轮询。会花钱，所以要
    走和排版一样的外壳（秒回 task_id），不能同步干等——前端 http 超时只有 60s。
  - `POST /apply`：把改写结果写回源文。**只有这一步改库**，前面两步都不动用户数据。

权限：全部 `Depends(current_owner_id)`（登录即可）。扫描与改写都不落库，
apply 只改 owner 自己的内容（service 层二次校验 owner）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.services import humanize as humanize_svc

router = APIRouter(prefix="/humanize", tags=["人工痕迹"])

# 单篇体检/改写的正文上限。10 万字远超一篇自媒体稿，主要是挡住误传整本书的情况。
TEXT_MAX = 100_000


class ScanRequest(BaseModel):
    content_id: str = ""
    text: str = ""
    platform: str = "gzh"


class RewriteRequest(BaseModel):
    content_id: str
    platform: str = "gzh"
    level: str = ""


class ApplyRequest(BaseModel):
    content_id: str
    text: str


@router.post("/scan")
async def scan(
    req: ScanRequest,
    owner: str = Depends(current_owner_id),
    db: AsyncSession = Depends(get_db),
):
    """体检一段文案的人工痕迹（零 LLM、零成本）。

    content_id 与 text 至少给一个：给了 content_id 就取其**源文**（content.body，
    Markdown），给了 text 就用传入的文本（前端传正在编辑的内容时用这个）。
    """
    from app.models.content import Content

    text = req.text
    platform = req.platform
    if not text and req.content_id:
        c = await db.get(Content, req.content_id)
        if c is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        if (c.owner or "default") != owner:
            raise HTTPException(status_code=404, detail="内容不存在")
        text = c.body or ""
        platform = platform or (c.platform or "gzh")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="没有可检查的正文")
    if len(text) > TEXT_MAX:
        raise HTTPException(status_code=413, detail=f"正文超过 {TEXT_MAX} 字上限")

    return humanize_svc.scan(text, platform)


@router.post("/rewrite")
async def rewrite(
    req: RewriteRequest,
    owner: str = Depends(current_owner_id),
    db: AsyncSession = Depends(get_db),
):
    """提交「加痕迹」改写任务，秒回 task_id。

    level 留空 = 按平台自动推荐（知乎轻度 / 公众号中度 / 小红书重度）。
    """
    from app.models.content import Content

    c = await db.get(Content, req.content_id)
    if c is None or (c.owner or "default") != owner:
        raise HTTPException(status_code=404, detail="内容不存在")
    if not (c.body or "").strip():
        raise HTTPException(status_code=400, detail="源文是空的，没有可改写的内容")

    platform = req.platform or (c.platform or "gzh")
    return await humanize_svc.submit_humanize_job(req.content_id, platform, req.level)


@router.get("/job/{task_id}")
async def get_job(task_id: str, owner: str = Depends(current_owner_id)):
    """轮询改写任务。result = {text, before_score, after_score, warnings, level}。"""
    job = humanize_svc.get_humanize_job(task_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "task_id": job["task_id"],
        "content_id": job["content_id"],
        "platform": job["platform"],
        "level": job["level"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "result": job["result"],
        "error": job["error"],
    }


@router.post("/apply")
async def apply(
    req: ApplyRequest,
    owner: str = Depends(current_owner_id),
    db: AsyncSession = Depends(get_db),
):
    """把改写结果写回源文（唯一的写操作）。

    排版产物会一并失效（它是从旧源文生成的），状态回退到未排版，**不自动重排**——
    那是另一笔模型开销，得用户点头。
    """
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="改写结果是空的")
    res = await humanize_svc.apply_humanized(db, req.content_id, req.text, owner=owner)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "写回失败"))
    return res


@router.get("/levels")
async def levels(owner: str = Depends(current_owner_id)):
    """强度档与平台默认值，给前端下拉用（不写死在前端，改档位时不用发版）。"""
    return {
        "levels": [
            {"id": k, "label": humanize_svc.LEVEL_LABELS[k], "desc": humanize_svc.LEVEL_DESC[k]}
            for k in humanize_svc.LEVELS
        ],
        "platform_default": humanize_svc.PLATFORM_DEFAULT_LEVEL,
    }
