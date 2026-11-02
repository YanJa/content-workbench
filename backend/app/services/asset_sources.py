"""在线图源适配器：Pixabay / Openverse。

为什么是这两家（结论来自实际条款核实，见 docs/素材库模块调研与实现方案.md 第 3 节）：

    Pixabay   —— 条款**要求**把图下载到自己的服务器（禁止永久热链），是唯一
                 既能免费商用、又明确允许自存的源。做主力。
    Openverse —— CC 授权聚合，支持只筛「允许商用」的许可。做合规补充。
    Unsplash  —— API 强制热链，不允许转存。只能在线引用，不进素材库。
    Pexels    —— 新 API Key 已停止发放，暂时接不了。

两个工程约束：

1. **结果必须缓存**。Pixabay 条款原文要求「requests must be cached for 24 hours」。
   这里用进程内 LRU + TTL，够了：搜索是低频动作，重启丢缓存也只多打一次请求。
2. **下载要防 SSRF**。Openverse 的图片托管在各家 CDN 上，域名无法白名单化，
   所以改查「目标是不是内网地址」+ 禁跳转 + 限流式读取（见 `download`）。
"""
from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.core.logging import logger

# ---------------------------------------------------------------- 源站常量
_PIXABAY_API = "https://pixabay.com/api/"
_PIXABAY_LICENSE_URL = "https://pixabay.com/service/license-summary/"
_OPENVERSE_API = "https://api.openverse.org/v1/images/"
_OPENVERSE_LICENSE = {
    "cc0": "CC0（公有领域，无需署名）",
    "pdm": "公有领域标记",
    "by": "CC BY（需署名）",
    "by-sa": "CC BY-SA（需署名 + 相同方式共享）",
}

# 缓存：key -> (expire_at, payload)。上限 200 条，超了丢最旧的。
_CACHE: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_CACHE_TTL = 24 * 3600
_CACHE_MAX = 200


def _cache_get(key: str) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    expire, payload = item
    if expire < time.time():
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)  # LRU：被用到就挪到队尾
    return payload


def _cache_set(key: str, payload: Any) -> None:
    _CACHE[key] = (time.time() + _CACHE_TTL, payload)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


# ---------------------------------------------------------------- 可用性
def source_status() -> list[dict[str, Any]]:
    """各图源是否可用。前端据此显示 tab 与「去配 key」提示，而不是让搜索报错。"""
    return [
        {
            "name": "pixabay",
            "label": "Pixabay",
            "configured": bool(settings.PIXABAY_API_KEY),
            "note": "可下载自存 · 免费商用 · 无需署名",
            "hint": "" if settings.PIXABAY_API_KEY else "在 .env 配 PIXABAY_API_KEY 后可用",
        },
        {
            "name": "openverse",
            "label": "Openverse",
            "configured": True,  # 匿名也能用（100 次/天）；配 token 只是提额度
            "note": "CC 授权 · 已过滤为允许商用",
            "hint": "",
        },
    ]


# ---------------------------------------------------------------- 标准化
def _pixabay_item(hit: dict) -> dict[str, Any]:
    user = hit.get("user") or ""
    user_id = hit.get("user_id")
    tags = [t.strip() for t in (hit.get("tags") or "").split(",") if t.strip()]
    return {
        "source_name": "pixabay",
        "source_id": str(hit.get("id") or ""),
        "title": (tags[0] if tags else "Pixabay 素材")[:300],
        "thumb_url": hit.get("previewURL") or hit.get("webformatURL") or "",
        "full_url": hit.get("largeImageURL") or hit.get("webformatURL") or "",
        "width": int(hit.get("imageWidth") or 0),
        "height": int(hit.get("imageHeight") or 0),
        "author": user,
        "author_url": f"https://pixabay.com/users/{user}-{user_id}/" if user_id else "",
        "license": "pixabay-content",
        "license_url": _PIXABAY_LICENSE_URL,
        "attribution_text": f"Image by {user} on Pixabay" if user else "",
        "source_url": hit.get("pageURL") or "",
        "tags": tags[:12],
        "mime": "image/jpeg",
    }


def _openverse_item(r: dict) -> dict[str, Any]:
    lic = (r.get("license") or "").lower()
    return {
        "source_name": "openverse",
        "source_id": str(r.get("id") or ""),
        "title": (r.get("title") or "Openverse 素材")[:300],
        "thumb_url": r.get("thumbnail") or r.get("url") or "",
        "full_url": r.get("url") or "",
        "width": int(r.get("width") or 0),
        "height": int(r.get("height") or 0),
        "author": r.get("creator") or "",
        "author_url": r.get("creator_url") or "",
        "license": lic or "unknown",
        "license_url": r.get("license_url") or "",
        # Openverse 直接给了格式化的署名行，别自己拼（各家 CC 要求不一样）
        "attribution_text": r.get("attribution") or "",
        "source_url": r.get("foreign_landing_url") or "",
        "tags": [t.get("name") for t in (r.get("tags") or []) if t.get("name")][:12],
        "mime": f"image/{r.get('filetype')}" if r.get("filetype") else "",
        "license_label": _OPENVERSE_LICENSE.get(lic, lic.upper()),
    }


# ---------------------------------------------------------------- 搜索
async def _pixabay_search(q: str, page: int, per_page: int) -> list[dict]:
    if not settings.PIXABAY_API_KEY:
        return []
    cache_key = f"pixabay:{q}:{page}:{per_page}"
    hit_cache = _cache_get(cache_key)
    if hit_cache is not None:
        return hit_cache

    async with httpx.AsyncClient(timeout=settings.ASSET_SEARCH_TIMEOUT) as cli:
        resp = await cli.get(
            _PIXABAY_API,
            params={
                "key": settings.PIXABAY_API_KEY,
                "q": q,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": per_page,
                "page": page,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    items = [_pixabay_item(h) for h in (data.get("hits") or [])]
    _cache_set(cache_key, items)
    return items


async def _openverse_search(q: str, page: int, per_page: int) -> list[dict]:
    cache_key = f"openverse:{q}:{page}:{per_page}"
    hit_cache = _cache_get(cache_key)
    if hit_cache is not None:
        return hit_cache

    headers = {"User-Agent": "content-workbench/1.0 (material library)"}
    if settings.OPENVERSE_TOKEN:
        headers["Authorization"] = f"Bearer {settings.OPENVERSE_TOKEN}"
    async with httpx.AsyncClient(timeout=settings.ASSET_SEARCH_TIMEOUT) as cli:
        resp = await cli.get(
            _OPENVERSE_API,
            params={
                "q": q,
                "page": page,
                "page_size": per_page,
                # 只取「允许商用」的：素材库的用途是给公众号/头条配图，那是商用场景。
                # 不筛的话会混进 CC-BY-NC 的图，用出去就是侵权。
                "license_type": "commercial",
                "mature": "false",
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    items = [_openverse_item(r) for r in (data.get("results") or [])]
    _cache_set(cache_key, items)
    return items


async def search(
    q: str, source: str = "all", page: int = 1, per_page: int = 20
) -> tuple[list[dict], list[dict]]:
    """聚合搜索。返回 `(items, errors)`。

    单个源失败不影响另一个 —— 用户要的是「能选到图」，不是「源健康报告」。
    但失败**必须回传**：全源都挂时如果只返回空列表，前端只能提示「没搜到这个
    关键词」，把「网络不通 / 没配 key」说成「库里没有」，会让人一直换关键词
    白试。所以这里把每个源的失败原因带出去，由前端照实说。
    """
    items: list[dict] = []
    errors: list[dict] = []

    async def _run(name: str, fn) -> None:
        try:
            items.extend(await fn(q, page, per_page))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 搜索失败: %s", name, exc)
            errors.append({"source": name, "reason": _err_text(exc)})

    if source in ("all", "pixabay"):
        await _run("pixabay", _pixabay_search)
    if source in ("all", "openverse"):
        await _run("openverse", _openverse_search)
    # 没有原图地址的条目直接丢：导入了也是坏数据
    return [i for i in items if i.get("full_url")], errors


def _err_text(exc: Exception) -> str:
    """把异常翻成一句人话。前端要直接显示，不能是 `TimeoutError()` 这种。"""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "连接超时（网络不通或图源被墙）"
    if isinstance(exc, httpx.ConnectError):
        return "连不上图源服务器"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return "API Key 无效或已过期"
        if code == 429:
            return "请求太频繁，被图源限流了（稍等一会儿）"
        return f"图源返回 {code}"
    if isinstance(exc, (KeyError, ValueError)):
        return "图源返回的数据格式变了"
    text = str(exc).strip()
    return text[:80] if text else "未知错误"


# ---------------------------------------------------------------- 按 ID 回查
async def fetch_one(source_name: str, source_id: str) -> dict | None:
    """按 ID 回源站查一条。

    存在的意义是**安全**：导入接口只收 source_id，真实下载地址由后端自己查出来。
    若信任前端传来的 URL，这个接口就成了 SSRF 跳板。
    """
    try:
        if source_name == "pixabay":
            if not settings.PIXABAY_API_KEY:
                return None
            async with httpx.AsyncClient(timeout=settings.ASSET_SEARCH_TIMEOUT) as cli:
                resp = await cli.get(
                    _PIXABAY_API,
                    params={"key": settings.PIXABAY_API_KEY, "id": source_id},
                )
                resp.raise_for_status()
                hits = resp.json().get("hits") or []
            return _pixabay_item(hits[0]) if hits else None

        if source_name == "openverse":
            headers = {"User-Agent": "content-workbench/1.0 (material library)"}
            if settings.OPENVERSE_TOKEN:
                headers["Authorization"] = f"Bearer {settings.OPENVERSE_TOKEN}"
            async with httpx.AsyncClient(timeout=settings.ASSET_SEARCH_TIMEOUT) as cli:
                resp = await cli.get(
                    f"{_OPENVERSE_API}{source_id}/", headers=headers
                )
                resp.raise_for_status()
                return _openverse_item(resp.json())
    except Exception as exc:  # noqa: BLE001
        logger.warning("图源回查失败 %s/%s: %s", source_name, source_id, exc)
    return None


# ---------------------------------------------------------------- 下载
def _assert_public_https(url: str) -> None:
    """下载前的 SSRF 闸门。

    Openverse 的图片散落在各 CDN 上，域名白名单没法穷举，所以反过来查：
    https + 不能是 IP 字面量 + 不能指向内网/环回/链路本地。

    IP 字面量直接拒，是因为正常图源都用域名；而 DNS 解析后的真实 IP
    在这里查会引入 TOCTOU 窗口（查完到 httpx 再解析之间可以变），
    与其给个看起来严谨其实拦不住的保护，不如把口子收窄到「只走域名」。
    """
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("只允许 https 素材地址")
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("素材地址缺少主机名")
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("拒绝内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 是域名，放行
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("拒绝内网地址")


async def download(url: str) -> bytes:
    """下载原图，带体积上限。

    用 stream 逐块读而不是 `resp.content`：一张 40MB 的原图会把内存打满，
    而且我们本来只需要前 20MB 就能判定「太大，不要了」。
    """
    _assert_public_https(url)
    limit = max(1, settings.ASSET_MAX_MB) * 1024 * 1024
    buf = bytearray()
    async with httpx.AsyncClient(
        timeout=settings.ASSET_SEARCH_TIMEOUT,
        # 不跟随跳转：3xx 可以跳到内网地址，绕开上面的检查
        follow_redirects=False,
    ) as cli:
        async with cli.stream(
            "GET", url, headers={"User-Agent": "content-workbench/1.0 (material library)"}
        ) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"下载失败 HTTP {resp.status_code}")
            async for chunk in resp.aiter_bytes(65536):
                buf.extend(chunk)
                if len(buf) > limit:
                    raise ValueError(f"素材超过 {settings.ASSET_MAX_MB}MB 上限，已放弃下载")
    return bytes(buf)
