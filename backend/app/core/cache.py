"""缓存层：Redis 优先，未配置时降级为进程内内存缓存。

设计意图：开发期不想装 Redis 也能跑；生产配了 REDIS_URL 自动切换。
两者接口完全一致（get / set / delete / exists）。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import settings
from app.core.logging import logger

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None


class MemoryCache:
    """进程内缓存（单机开发用，多进程不共享）。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    async def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        raw, expire_at = item
        if expire_at and time.time() > expire_at:
            self._data.pop(key, None)
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expire_at = time.time() + ttl if ttl else None
        self._data[key] = (json.dumps(value, ensure_ascii=False), expire_at)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    """Redis 缓存。"""

    def __init__(self, url: str) -> None:
        self._client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(key)
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._client.set(
            key, json.dumps(value, ensure_ascii=False), ex=ttl or settings.REDIS_TTL
        )

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(key))

    async def close(self) -> None:
        await self._client.aclose()


_cache: MemoryCache | RedisCache | None = None


def get_cache() -> MemoryCache | RedisCache:
    global _cache
    if _cache is not None:
        return _cache

    if settings.use_redis and aioredis is not None:
        try:
            _cache = RedisCache(settings.REDIS_URL)
            logger.info("缓存层: Redis (%s)", settings.REDIS_URL)
            return _cache
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 初始化失败，降级为内存缓存: %s", exc)

    _cache = MemoryCache()
    logger.info("缓存层: 内存（未配置 Redis，单机开发模式）")
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
