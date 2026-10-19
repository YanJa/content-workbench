"""知乎热点路由。

给对话页「不知道写什么」的灵感卡片供数据，顺带一个状态页用的自查端点。

端点分工：

| 端点 | 权限 | 为什么是这个权限 |
|---|---|---|
| `GET /zhihu/hot` | 登录即可 | 灵感是给所有创作者的，不必要也不该收窄给管理员 |
| `GET /zhihu/usage` | 仅管理员 | 额度余量与凭证状态是运营信息 |
| `PUT /zhihu/secret` | 仅管理员 | 改凭证 = 改系统行为，且保存即抓一次（花额度） |
| `POST /zhihu/refresh` | 仅管理员 | 手动刷新会**真金白银消耗共享额度池**，不能让人人都能烧 |
| `GET /zhihu/status` | 仅管理员 | 连通性自查（会消耗 1 次额度） |

注意：除 `POST /refresh` 与 `PUT /secret`（各花 1 次）外，所有读数都走本地库，
**不直接调知乎接口** —— 用户浏览热点不该扣额度，那是后台定时任务的事。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

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


@router.get("/usage")
async def usage(_admin: User = Depends(require_admin)):
    """凭证状态 + 当日额度余量 + 上次抓取结果（管理后台「知乎热点」卡片）。

    只读本地库，**不消耗知乎额度** —— 与 `/status`（要花 1 次验连通）分工明确。
    """
    u = await zhihu.usage_today()
    snap = await zhihu.last_snapshot_info()
    return {
        **zhihu.secret_info(),
        **u,
        "interval_minutes": settings.ZHIHU_HOT_INTERVAL_MINUTES,
        "last_fetch_at": snap["fetched_at"],
        "last_ok": snap["ok"],
        "last_item_count": snap["item_count"],
    }


class SecretBody(BaseModel):
    """传空串 = 清除凭证。"""

    secret: str = ""


@router.put("/secret")
async def put_secret(body: SecretBody, _admin: User = Depends(require_admin)):
    """设置 / 清除 Access Secret（落库 + 改内存，不用重启），并立即抓一次。

    保存后顺手抓一次是为了**立刻验证凭证对不对**：抓失败的会把原因显示在
    「上次抓取」里，不用等半小时后的定时任务才发现钥匙是坏的。
    """
    await zhihu.set_runtime_secret(body.secret)
    fetch = await zhihu.fetch_hot_once() if zhihu._configured() else None
    u = await zhihu.usage_today()
    snap = await zhihu.last_snapshot_info()
    return {
        **zhihu.secret_info(),
        **u,
        "interval_minutes": settings.ZHIHU_HOT_INTERVAL_MINUTES,
        "last_fetch_at": snap["fetched_at"],
        "last_ok": snap["ok"],
        "last_item_count": snap["item_count"],
        "fetch": fetch,
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
