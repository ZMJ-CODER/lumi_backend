"""高频 API 读取使用的用户范围视图缓存与延迟辅助函数。

缓存刻意与 ``mem:user:*`` 分离：后者存放提示词注入数据，而 API 响应负载拥有不
同的数据结构和 TTL。Redis 仅是优化；任意失败都失败开放至数据库。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable
from typing import Any, TypeVar

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.observability import inc_read_view_cache, observe_read_view_stage
from app.core.redis import get_redis


T = TypeVar("T")

MEMORY_VIEW_PREFIX = "api:view:memory"
CONVERSATION_VIEW_PREFIX = "api:view:conversations"
USER_VIEW_PREFIX = "api:view:user"


class ReadViewTimer:
    """Keep endpoint-stage labels bounded while measuring a read request."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    async def checkout(self, db: AsyncSession) -> None:
        started = time.perf_counter()
        try:
            # Force lazy SQLAlchemy checkout now so pool wait is measured separately.
            await db.connection()
        finally:
            observe_read_view_stage(self.endpoint, "db_checkout", time.perf_counter() - started)

    async def query(self, operation: Awaitable[T]) -> T:
        started = time.perf_counter()
        try:
            return await operation
        finally:
            observe_read_view_stage(self.endpoint, "sql", time.perf_counter() - started)

    def observe(self, stage: str, started: float) -> None:
        observe_read_view_stage(self.endpoint, stage, time.perf_counter() - started)


def memory_view_key(user_id: str) -> str:
    return f"{MEMORY_VIEW_PREFIX}:{user_id}"


def user_view_key(user_id: str) -> str:
    return f"{USER_VIEW_PREFIX}:{user_id}"


def conversation_view_key(user_id: str, *, scene: str, limit: int, offset: int) -> str:
    params = json.dumps({"scene": scene, "limit": limit, "offset": offset}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(params.encode("utf-8")).hexdigest()[:16]
    return f"{CONVERSATION_VIEW_PREFIX}:{user_id}:{digest}"


async def get_read_view(key: str, *, endpoint: str, timer: ReadViewTimer) -> dict[str, Any] | None:
    """Read a JSON response cache entry, treating Redis issues as cache misses."""
    if not settings.READ_VIEW_CACHE_ENABLED:
        inc_read_view_cache(endpoint, "disabled")
        return None

    started = time.perf_counter()
    try:
        raw = await get_redis().get(key)
        if raw is None:
            inc_read_view_cache(endpoint, "miss")
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("cached read view is not an object")
        inc_read_view_cache(endpoint, "hit")
        return value
    except Exception as exc:  # noqa: BLE001
        inc_read_view_cache(endpoint, "error")
        logger.debug("读取用户视图缓存失败: {}", exc)
        return None
    finally:
        timer.observe("cache_get", started)


async def set_read_view(
    key: str,
    value: dict[str, Any],
    *,
    endpoint: str,
    ttl_seconds: int,
    timer: ReadViewTimer,
) -> None:
    """Cache a complete response body after a successful database read."""
    if not settings.READ_VIEW_CACHE_ENABLED or ttl_seconds <= 0:
        return

    started = time.perf_counter()
    try:
        await get_redis().set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ex=ttl_seconds)
        inc_read_view_cache(endpoint, "store")
    except Exception as exc:  # noqa: BLE001
        inc_read_view_cache(endpoint, "store_error")
        logger.debug("写入用户视图缓存失败: {}", exc)
    finally:
        timer.observe("cache_set", started)


async def invalidate_memory_view(user_id: str) -> None:
    """Invalidate only the API memory view; prompt-injection cache stays separate."""
    try:
        await get_redis().delete(memory_view_key(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("清理记忆视图缓存失败: {}", exc)


async def invalidate_user_view(user_id: str) -> None:
    """Invalidate the cached public profile after a profile, role or status change."""
    try:
        await get_redis().delete(user_view_key(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("清理用户视图缓存失败: {}", exc)


async def invalidate_conversation_views(user_id: str) -> None:
    """Invalidate all paginated views for one user without Redis KEYS blocking."""
    try:
        redis = get_redis()
        pattern = f"{CONVERSATION_VIEW_PREFIX}:{user_id}:*"
        keys = [key async for key in redis.scan_iter(match=pattern, count=100)]
        if keys:
            await redis.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.debug("清理会话列表缓存失败: {}", exc)
