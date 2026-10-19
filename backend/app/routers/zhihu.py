"""知乎热点路由。

给对话页「不知道写什么」的灵感卡片供数据，顺带一个状态页用的自查端点。

端点分工：

| 端点 | 权限 | 为什么是这个权限 |
|---|---|---|
| `GET /zhihu/hot` | 登录即可 | 灵感是给所有创作者的，不必要也不该收窄给管理员 |
| `POST /zhihu/refresh` | 仅管理员 | 手动刷新会**真金白银消耗共享额度池**，不能让人人都能烧 |
| `GET /zhihu/status` | 仅管理员 | 含额度用量、连通性这类运营信息 |

注意：所有读数都走本地库，**不直接调知乎接口** —— 用户浏览热点不该扣额度，
那是后台定时任务的事。唯一的例外是 `POST /zhihu/refresh`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.rbac import current_user, require_admin
from app.core.config import settings
from app.models.user import User
from app.services import zhihu

router = APIRouter(prefix="/zhihu", tags=["知乎热点"])


@router.get("/hot")
async def hot_list(
    limit: int = Query(20, ge=1, le=30),
    sort: str = Query("trend", pattern="^(trend|rank)$"),
    _user: User = Depends(current_user),
):
    """当前知乎热榜 + 趋势信号。

    `sort=trend`（默认）按 heat_score 排，把「正在往上冲」的顶到前面；
    `sort=rank` 按榜单原名次排。
    """
    res = await zhihu.hot_with_trend(limit=limit)
    if not res["ok"]:
        return {**res, "configured": zhihu._configured(), "interval_minutes": settings.ZHIHU_HOT_INTERVAL_MINUTES}
    items = res["items"]
    if sort == "rank":
        items = sorted(items, key=lambda x: x["rank"])
    return {
        "ok": True,
        "fetched_at": res["fetched_at"],
        "interval_minutes": settings.ZHIHU_HOT_INTERVAL_MINUTES,
        "items": items,
    }


@router.post("/refresh")
async def refresh_hot(_admin: User = Depends(require_admin)):
    """手动抓一次热榜（`force=True`，跳过额度水位保护）。

    后台定时任务本身会跑，这个端点是给「我等不及下一轮」用的 —— 所以只给
    管理员，且跳过保护仅限此处：定时抓取仍然受水位约束。
    """
    res = await zhihu.fetch_hot_once(force=True)
    usage = await zhihu.usage_today()
    return {**res, "usage": usage}


@router.get("/status")
async def status(_admin: User = Depends(require_admin)):
    """连通性与额度自查（系统状态页用）。

    注意：调用这个端点会消耗 1 次额度（必须真实请求才能验通）。额度紧张时
    少点几次，或者直接看返回的 usage 就知道。
    """
    return await zhihu.check()
