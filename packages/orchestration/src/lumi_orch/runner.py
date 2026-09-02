"""与运行时后端无关的节点超时选择逻辑。"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Mapping


CHANNEL_ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1]); local limit = tonumber(ARGV[2]); local lease = tonumber(ARGV[3]); local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
redis.call('ZADD', KEYS[1], now + lease, token)
redis.call('EXPIRE', KEYS[1], lease)
return 1
"""

CHANNEL_RENEW_SCRIPT = """
local now = tonumber(ARGV[1]); local lease = tonumber(ARGV[2]); local token = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], token) == false then return 0 end
redis.call('ZADD', KEYS[1], 'XX', now + lease, token)
redis.call('EXPIRE', KEYS[1], lease)
return 1
"""

RedisProvider = Callable[[], Awaitable[Any | None] | Any | None]
ChannelLimitProvider = Callable[[str], int]
WaitObserver = Callable[[str, float], None]


def resolve_node_timeout(
    node: Any,
    *,
    default_seconds: int,
    channel_timeouts: Mapping[str, int],
    tool_timeouts: Mapping[str, int],
) -> int:
    """Choose a positive timeout without reading global configuration."""
    fallback = max(1, int(default_seconds))
    params = _node_mapping(node, "params")
    tool = str(params.get("preferred_tool") or "")
    tool_timeout = tool_timeouts.get(tool)
    if tool_timeout is not None:
        try:
            return max(1, int(tool_timeout))
        except (TypeError, ValueError):
            pass
    channel = str(_node_mapping(node, "metadata").get("route_channel") or "agent")
    try:
        configured = int(channel_timeouts.get(channel, 0))
    except (TypeError, ValueError):
        configured = 0
    return max(1, configured) if configured > 0 else fallback


def _node_mapping(node: Any, field: str) -> dict[str, Any]:
    value = node.get(field) if isinstance(node, dict) else getattr(node, field, None)
    return value if isinstance(value, dict) else {}


class ChannelLimiter:
    """Cross-process channel leases with process-local semaphore fallback."""

    def __init__(
        self,
        *,
        redis_provider: RedisProvider | None = None,
        limit_provider: ChannelLimitProvider | None = None,
        wait_observer: WaitObserver | None = None,
    ) -> None:
        self._redis_provider = redis_provider
        self._limit_provider = limit_provider or (lambda _channel: 1)
        self._wait_observer = wait_observer or (lambda _channel, _seconds: None)
        self._semaphores: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()

    async def _redis(self) -> Any | None:
        if self._redis_provider is None:
            return None
        result = self._redis_provider()
        return await result if inspect.isawaitable(result) else result

    def _limit(self, channel: str) -> int:
        return max(1, int(self._limit_provider(channel)))

    def _semaphore(self, channel: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        per_loop = self._semaphores.get(loop)
        if per_loop is None:
            per_loop = {}
            self._semaphores[loop] = per_loop
        semaphore = per_loop.get(channel)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._limit(channel))
            per_loop[channel] = semaphore
        return semaphore

    def _observe_wait(self, channel: str, seconds: float) -> None:
        try:
            self._wait_observer(channel, seconds)
        except Exception:  # noqa: BLE001
            pass

    @asynccontextmanager
    async def claim(self, channel: str, *, lease_seconds: int = 360):
        channel = str(channel or "agent")
        token = uuid.uuid4().hex
        key = f"agent:channel:{channel}"
        redis = None
        renew_task: asyncio.Task[None] | None = None
        holder = asyncio.current_task()
        lease_seconds = max(60, int(lease_seconds))
        started = time.perf_counter()
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("channel lease backend unavailable")
            while not await redis.eval(
                CHANNEL_ACQUIRE_SCRIPT, 1, key, time.time(), self._limit(channel), lease_seconds, token
            ):
                await asyncio.sleep(0.05)
        except Exception:  # noqa: BLE001
            semaphore = self._semaphore(channel)
            async with semaphore:
                self._observe_wait(channel, time.perf_counter() - started)
                yield
            return

        async def renew_loop() -> None:
            interval = max(1.0, min(30.0, lease_seconds / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await redis.eval(CHANNEL_RENEW_SCRIPT, 1, key, time.time(), lease_seconds, token)
                except Exception:  # noqa: BLE001
                    renewed = 0
                if int(renewed or 0) != 1:
                    if holder is not None and not holder.done():
                        holder.cancel()
                    return

        try:
            renew_task = asyncio.create_task(renew_loop())
            self._observe_wait(channel, time.perf_counter() - started)
            yield
        finally:
            if renew_task is not None:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
            try:
                await redis.zrem(key, token)
            except Exception:  # noqa: BLE001
                # Expiry is the safe backstop if Redis fails after acquisition.
                pass
