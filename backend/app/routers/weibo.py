"""微博热搜路由（灵感模块的第二个热点源，方案 A）。

端点：
  GET /weibo/hot  登录即可 —— 灵感给所有创作者，且聚合接口免费、不消耗知乎
                  额度，没必要收窄给管理员。后端实时拉取（带 60s 内存缓存），
                  不入库。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.rbac import current_user
from app.models.user import User
from app.services import weibo

router = APIRouter(prefix="/weibo", tags=["微博热搜"])


@router.get("/hot")
async def hot_list(
    limit: int = Query(20, ge=1, le=50),
    _user: User = Depends(current_user),
):
    """微博实时热搜榜（免费聚合接口，登录即可）。"""
    res = await weibo.weibo_hot(limit=limit)
    return {"source": "weibo", **res}
