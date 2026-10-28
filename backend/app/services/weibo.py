"""微博热搜抓取（方案 A：第三方聚合 hotboard，免费、无需 token）。

数据来自 uapis.cn 的 /api/v1/misc/hotboard?type=weibo —— 这是把微博官方热搜
二次聚合出来的公共服务，**免费、无需鉴权**。本模块只做「实时拉取 + 轻量内存
缓存」，不入库、不加定时任务（最小可用版）：

  - 灵感场景对新鲜度要求不高（和知乎热榜一样，半小时级延迟可接受）；
  - 聚合接口免费且不限 token，重复拉的成本只是一次出网请求，TTL 缓存兜底即可。

⚠ 这是第三方小服务，SLA 不保证，仅用于选题灵感，不做关键业务依赖。日后要
长期做微博选题，应换成微博官方开放平台 API（需过应用审核拿 OAuth token）。
"""
from __future__ import annotations

import time

import httpx

from app.core.config import settings
from app.core.logging import logger

API = "https://uapis.cn"
_HOTBOARD = "/api/v1/misc/hotboard"
# 内存缓存：同一会话反复切 tab 不该每次都打外部接口
_CACHE_TTL = 60
_cache: dict = {"ts": 0.0, "data": None}


async def weibo_hot(limit: int = 20) -> dict:
    """拉微博实时热搜榜。

    返回 {ok, items:[{rank,title,url,hot}], update_time, error?}。
    `hot` 是尽力归一的整数热度值（百万级），纯展示用。
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return {**_cache["data"], "cached": True}

    n = max(1, min(int(limit or 20), 50))
    try:
        async with httpx.AsyncClient(timeout=settings.ZHIHU_TIMEOUT) as client:
            resp = await client.get(
                f"{API}{_HOTBOARD}",
                params={"type": "weibo"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
        data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[weibo] 拉取热搜失败：%s", exc)
        return {"ok": False, "items": [], "error": f"微博热搜拉取失败：{exc}"}

    if resp.status_code != 200 or data.get("type") != "weibo":
        err = data.get("error") or f"聚合接口返回 HTTP {resp.status_code}"
        return {"ok": False, "items": [], "error": err}

    items = []
    for it in (data.get("list") or [])[:n]:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title:
            continue
        items.append(
            {
                "rank": int(it.get("index") or 0),
                "title": title,
                "url": url,
                "hot": _parse_hot(it.get("hot_value")),
            }
        )
    res = {"ok": True, "items": items, "update_time": data.get("update_time")}
    _cache["ts"] = now
    _cache["data"] = res
    return res


def _parse_hot(raw) -> int:
    """热度值可能是纯数字字符串 '1076578'，也可能带单位（如 '4880 万热度'）。

    尽力归一成整数；解析失败兜底为 0（纯展示，不精确无所谓）。
    """
    if isinstance(raw, (int, float)):
        return int(raw)
    s = (str(raw or "").replace("热度", "").replace(" ", "")).strip()
    # 单位换算：万→×1e4，亿→×1e8
    mult = 1
    if s.endswith("万"):
        mult, s = 10_000, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except Exception:
        return 0
