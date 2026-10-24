"""AI 用量路由：概览、明细、Top 会话、单价表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.core.pricing import pricing_state, pricing_table, reload_pricing
from app.core.tenant import current_owner_id
from app.db.session import get_db
from app.services import usage as usage_svc

router = APIRouter(prefix="/usage", tags=["AI用量"])


@router.get("/summary")
async def summary(
    days: int | None = Query(None, description="最近 N 天；与 start/end 二选一"),
    conversation_id: str | None = Query(None),
    start: int | None = Query(None, description="起始时间戳（秒）"),
    end: int | None = Query(None, description="结束时间戳（秒）"),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """总量 + 按天/按模型/按场景三条序列。

    一次返回，前端不用为同一张页面打三个接口。
    """
    return await usage_svc.summary(
        db, owner=owner, days=days, conversation_id=conversation_id, start=start, end=end
    )


@router.get("/records")
async def records(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    days: int | None = Query(None),
    conversation_id: str | None = Query(None),
    model: str | None = Query(None),
    scene: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """明细分页。"""
    items, total = await usage_svc.records(
        db,
        owner=owner,
        limit=limit,
        offset=offset,
        days=days,
        conversation_id=conversation_id,
        model=model,
        scene=scene,
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/top-conversations")
async def top_conversations(
    # 上限放到 36500（100 年）：「全部」场景前端会传一个大值。
    # 这里不做强约束是因为它只用于过滤，取多少天的数据规模都可控。
    days: int | None = Query(30, ge=1, le=36_500, description="最近 N 天；不传则不限"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    owner: str = Depends(current_owner_id),
):
    """消耗最高的会话，方便直接从用量页定位到是哪次对话烧的钱。"""
    return await usage_svc.top_conversations(db, owner=owner, days=days, limit=limit)


@router.get("/pricing")
async def pricing():
    """当前生效的单价表 + 加载来源。

    价格来源是可覆盖的（内置 → config/pricing.json → 环境变量 PRICING_JSON），
    所以这里从引擎实时读，不返回常量副本 —— 否则改了单价文件页面还显示旧价。
    """
    return {"items": pricing_table(), "state": pricing_state()}


@router.get("/estimate")
async def estimate(
    prompt: int = Query(0, ge=0),
    completion: int = Query(0, ge=0),
    cached: int = Query(0, ge=0),
    model: str = Query("deepseek-flash"),
    provider_key: str = Query(""),
):
    """估算一次调用的费用。

    给聊天页用 —— 每条回复流结束时把 tokens 丢过来换钱数。
    计价逻辑必须留在 Python 侧（峰谷、阶梯、别名都在引擎里），
    前端不重复实现，免得两处算出来不一样。

    provider_key 可选：带上它能认出「这家服务商是免费通道」的模型
    （比如 Cloudflare —— 它的模型免费但不一定登记过单价）。
    """
    from app.core.pricing import cost_breakdown
    from app.services import usage as usage_svc

    kind = usage_svc.billing_of(model, provider_key)
    if kind == "free":
        # 免费模型：把单价按 0 算，同时保留 token 明细，前端能显示「免费」
        out = cost_breakdown(model, prompt, completion, cached)
        out["billing"] = "free"
        out["cost"] = 0.0
        out["is_peak"] = False
        out["peak_multiplier"] = 1.0
        for p in out["parts"]:
            p["unit_price"] = 0.0
            p["cost"] = 0.0
        return out
    return cost_breakdown(model, prompt, completion, cached)


@router.post("/pricing/reload", tags=["AI用量"])
async def reload_prices(owner: str = Depends(current_owner_id)):
    """热加载单价表。

    往 config/pricing.json 里加了个新模型后不用重启后端：
    改完文件调一下这个接口即可。

    要求登录：这是会改进程内状态的管理动作，不能裸奔。
    """
    state = reload_pricing()
    logger.info("单价表已重新加载: %s", state)
    return {"ok": True, "state": state, "items": pricing_table()}
