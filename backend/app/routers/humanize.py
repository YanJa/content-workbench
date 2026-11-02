"""人工痕迹体检与「加痕迹」改写。

五个端点，成本各不同：
  - `POST /scan`：**零 LLM 零成本**。纯规则 + 统计，毫秒级。可以随便点。
  - `POST /zhuque`：**调外部 API**（腾讯朱雀，扣 Makers 免费 token 额度），
    同步返回三分类占比 + 分段 AIGC 值。
  - `POST /rewrite`：**走 LLM**（reasoning 档），异步任务 + 轮询。花钱，
    走和排版一样的外壳（秒回 task_id）。
  - `POST /apply`：把改写结果写回源文。**只有这一步改库**。
  - `POST /report`：生成「AI 生成检测报告单」HTML（朱雀 + 本地体检合并），
    朱雀未配置/失败时降级为纯本地报告。同步，会真调一次朱雀。

权限：全部 `Depends(current_owner_id)`（登录即可）。扫描、检测、报告都不落库。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.services import detect_report, humanize as humanize_svc, zhuque as zhuque_svc

router = APIRouter(prefix="/humanize", tags=["人工痕迹"])

# 单篇体检/改写的正文上限。10 万字远超一篇自媒体稿，主要是挡住误传整本书的情况。
TEXT_MAX = 100_000


def _pick_source(c: Any, source: str, platform: str) -> tuple[str, str]:
    """按「检测对象」取出要检的正文，返回 (正文, 实际检的口径)。

    - `layout`（排版产物，默认）：`layouts[platform].text`。发布出去的就是这一份
      （小红书/头条/百家号/公众号贴图粘的全是它），体检它才有意义。
    - `body`（源文 Markdown）：改写和写回都以它为准，想看「改写前后差在哪」时才用。

    排版产物不存在时**降级到源文**并把口径如实返回 —— 前端据此提示「还没排版，
    这次检的是源文」，不能默默检了源文却让用户以为是排版产物。
    """
    if source == "body":
        return c.body or "", "body"
    platform = platform or (c.platform or "gzh")
    lay = (c.layouts or {}).get(platform) or {}
    text = (lay.get("text") or "").strip()
    if text:
        return text, "layout"
    return c.body or "", "body"


class ScanRequest(BaseModel):
    content_id: str = ""
    text: str = ""
    platform: str = "gzh"
    sub_type: str = ""
    # 检测对象：layout=排版产物（默认），body=源文 Markdown
    source: str = "layout"


class RewriteRequest(BaseModel):
    content_id: str
    platform: str = "gzh"
    level: str = ""
    sub_type: str = ""


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

    正文来源优先级：传了 text 就用 text（前端正在编辑、未落库的内容）；
    否则按 `source` 取库里的排版产物或源文，见 `_pick_source`。
    返回里的 `source` 是**实际**检的口径，请求了排版产物但还没排版时会变回 body。
    """
    from app.models.content import Content
    from app.prompts.platforms import normalize_sub_type

    text = req.text
    platform = req.platform
    sub_type = req.sub_type
    source = req.source or "layout"
    if not text and req.content_id:
        c = await db.get(Content, req.content_id)
        if c is None:
            raise HTTPException(status_code=404, detail="内容不存在")
        if (c.owner or "default") != owner:
            raise HTTPException(status_code=404, detail="内容不存在")
        text, source = _pick_source(c, source, platform)
        platform = platform or (c.platform or "gzh")
        # 前端没传子形态时从内容兜底（队列里存的是权威值）
        sub_type = sub_type or normalize_sub_type(platform, c.sub_type)
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="没有可检查的正文")
    if len(text) > TEXT_MAX:
        raise HTTPException(status_code=413, detail=f"正文超过 {TEXT_MAX} 字上限")

    return {**humanize_svc.scan(text, platform, sub_type), "source": source}


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
    return await humanize_svc.submit_humanize_job(
        req.content_id, platform, req.level, req.sub_type, owner
    )


@router.get("/job/{task_id}")
async def get_job(task_id: str, owner: str = Depends(current_owner_id)):
    """轮询改写任务。result = {text, before_score, after_score, warnings, level}。

    - `indeterminate`：当前阶段的百分比不可信（模型调用中），前端该展示已用时长
      而不是死盯进度条；
    - `elapsed`：服务端算出的已用秒数（含排队）；
    - 任务只在内存里，**服务重启就没了** —— 前端收到 404 必须提示重试，
      不能静默重试到天荒地老。
    """
    job = humanize_svc.get_humanize_job(task_id, owner)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已失效（服务重启会清空任务）")
    return {
        "task_id": job["task_id"],
        "content_id": job["content_id"],
        "platform": job["platform"],
        "sub_type": job.get("sub_type", ""),
        "level": job["level"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "indeterminate": bool(job.get("indeterminate")),
        "elapsed": job.get("elapsed", 0),
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


class ZhuqueRequest(BaseModel):
    content_id: str = ""
    text: str = ""
    platform: str = ""
    source: str = "layout"


class ReportRequest(BaseModel):
    content_id: str = ""
    text: str = ""
    platform: str = ""
    sub_type: str = ""
    source: str = "layout"


@router.post("/zhuque")
async def zhuque_detect(
    req: ZhuqueRequest,
    owner: str = Depends(current_owner_id),
    db: AsyncSession = Depends(get_db),
):
    """朱雀 AI 生成检测（腾讯云 @makers/zhuque-text，消耗 Makers 免费额度）。

    is_merge=false：每段独立置信度，供面板的占比条与报告单的片段解析用。
    同步返回——单次调用通常几秒，不值得套异步 job 的外壳。

    检哪一份跟本地体检走同一个开关（`source`）：默认检排版产物，因为发出去的是它。
    """
    from app.models.content import Content

    text = req.text
    actual = req.source or "layout"
    if not text and req.content_id:
        c = await db.get(Content, req.content_id)
        if c is None or (c.owner or "default") != owner:
            raise HTTPException(status_code=404, detail="内容不存在")
        text, actual = _pick_source(c, req.source or "layout", req.platform)
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="没有可检测的正文")
    # 把实际检的口径带回去：前端要按它归档结果，避免「检的是源文、标的是排版产物」
    res = await zhuque_svc.classify(text)
    return {**res, "source": actual}


@router.post("/report", response_class=HTMLResponse)
async def detect_report_page(
    req: ReportRequest,
    owner: str = Depends(current_owner_id),
    db: AsyncSession = Depends(get_db),
):
    """生成「AI 生成检测报告单」HTML（朱雀同款版式 + 本地体检，可直接打印成 PDF）。

    朱雀未配置或调用失败时自动降级为纯本地体检报告，不会整个 500。
    报告标题会写明检的是**排版产物还是源文**——两张报告单长得一样却口径不同，
    分不清就会拿错分数去决策。
    """
    from app.models.content import Content

    text = req.text
    platform = req.platform
    sub_type = req.sub_type
    source = req.source or "layout"
    if not text and req.content_id:
        c = await db.get(Content, req.content_id)
        if c is None or (c.owner or "default") != owner:
            raise HTTPException(status_code=404, detail="内容不存在")
        text, source = _pick_source(c, source, platform)
        platform = platform or (c.platform or "")
        sub_type = sub_type or (c.sub_type or "")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="没有可检测的正文")

    scan = humanize_svc.scan(text, platform or "gzh", sub_type)
    zhuque_result = None
    if zhuque_svc.configured():
        zhuque_result = await zhuque_svc.classify(text)
    html_text = detect_report.render_report(
        text=text,
        scan=scan,
        zhuque_result=zhuque_result,
        platform=platform,
        sub_type=sub_type,
        source=source,
    )
    return HTMLResponse(content=html_text)


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
