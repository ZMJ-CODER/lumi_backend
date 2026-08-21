"""Cross-runtime concurrency limits for the four office routing channels."""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from contextlib import asynccontextmanager

from app.core.config import settings


_ACQUIRE = """
local now = tonumber(ARGV[1]); local limit = tonumber(ARGV[2]); local lease = tonumber(ARGV[3]); local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
redis.call('ZADD', KEYS[1], now + lease, token)
redis.call('EXPIRE', KEYS[1], lease)
return 1
"""

_RENEW = """
local now = tonumber(ARGV[1]); local lease = tonumber(ARGV[2]); local token = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], token) == false then return 0 end
redis.call('ZADD', KEYS[1], 'XX', now + lease, token)
redis.call('EXPIRE', KEYS[1], lease)
return 1
"""


def _limit(channel: str) -> int:
    return max(1, {
        "direct_llm": settings.AGENT_CHANNEL_DIRECT_LLM_CONCURRENCY,
        "deterministic_script": settings.AGENT_CHANNEL_SCRIPT_CONCURRENCY,
        "rag": settings.AGENT_CHANNEL_RAG_CONCURRENCY,
        "agent": settings.AGENT_CHANNEL_AGENT_CONCURRENCY,
    }.get(channel, settings.AGENT_CHANNEL_AGENT_CONCURRENCY))


class ChannelLimiter:
    """Redis leases in production, asyncio semaphores as safe local fallback."""

    def __init__(self) -> None:
        self._semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _semaphore(self, channel: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        per_loop = self._semaphores.get(loop)
        if per_loop is None:
            per_loop = {}
            self._semaphores[loop] = per_loop
        sem = per_loop.get(channel)
        if sem is None:
            sem = asyncio.Semaphore(_limit(channel))
            per_loop[channel] = sem
        return sem

    @asynccontextmanager
    async def claim(self, channel: str, *, lease_seconds: int = 360) -> None:
        channel = str(channel or "agent")
        token = uuid.uuid4().hex
        key = f"agent:channel:{channel}"
        redis = None
        renew_task: asyncio.Task | None = None
        holder = asyncio.current_task()
        lease_seconds = max(60, int(lease_seconds))
        started = time.perf_counter()
        try:
            from app.core.redis import get_redis

            redis = get_redis()
            while not await redis.eval(_ACQUIRE, 1, key, time.time(), _limit(channel), lease_seconds, token):
                await asyncio.sleep(0.05)
        except Exception:
            # Redis may be unavailable during local startup. The process-local
            # fallback still protects the API worker from self-inflicted bursts.
            sem = self._semaphore(channel)
            async with sem:
                self._observe_wait(channel, time.perf_counter() - started)
                yield
            return
        async def renew_loop() -> None:
            # A ZSET expiry is only a crash backstop. Long nodes must keep
            # proving ownership, otherwise another worker could be admitted
            # while this holder is still issuing an external request.
            interval = max(1.0, min(30.0, lease_seconds / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await redis.eval(_RENEW, 1, key, time.time(), lease_seconds, token)
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
            except Exception:
                # Lease expiry is the safe backstop if Redis becomes
                # unavailable after a successful acquire.
                pass

    @staticmethod
    def _observe_wait(channel: str, seconds: float) -> None:
        try:
            from app.core.observability import observe_agent_channel_wait

            observe_agent_channel_wait(channel, seconds)
        except Exception:  # noqa: BLE001
            pass


channel_limiter = ChannelLimiter()
