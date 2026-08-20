"""用户级令牌桶限流。仅在请求开始时消费一次，SSE 输出分片不会重复扣额。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from fastapi import Request

from app.core.config import settings


@dataclass(frozen=True)
class ThrottleResult:
    allowed: bool
    retry_after: int = 0


_local_buckets: dict[str, tuple[float, float]] = {}
_local_lock = asyncio.Lock()


class TokenBucketLimiter:
    _LUA = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local raw = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local tokens = tonumber(raw[1])
local updated = tonumber(raw[2])
if not tokens then tokens = capacity end
if not updated then updated = now end
tokens = math.min(capacity, tokens + math.max(0, now - updated) * refill)
if tokens < cost then
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
  redis.call('EXPIRE', KEYS[1], ttl)
  return {0, math.ceil((cost - tokens) / refill)}
end
tokens = tokens - cost
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], ttl)
return {1, 0}
"""

    def __init__(self, group: str, capacity: int, refill_per_minute: float) -> None:
        self.group = group
        self.capacity = max(1, int(capacity))
        self.refill_per_second = max(0.001, float(refill_per_minute) / 60.0)

    @staticmethod
    def _key(identity: str, group: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return f"rl:bucket:{group}:{digest}"

    async def consume(self, identity: str, *, cost: int = 1) -> ThrottleResult:
        now = time.time()
        key = self._key(identity, self.group)
        ttl = max(60, int(self.capacity / self.refill_per_second * 2))
        try:
            from app.core.redis import get_redis

            data = await get_redis().eval(
                self._LUA, 1, key, self.capacity, self.refill_per_second, now, cost, ttl
            )
            return ThrottleResult(bool(int(data[0])), int(data[1]))
        except Exception:
            # Redis 不可用时仍提供单 worker 保护，避免高峰瞬间完全失守。
            async with _local_lock:
                tokens, updated = _local_buckets.get(key, (float(self.capacity), now))
                tokens = min(self.capacity, tokens + max(0.0, now - updated) * self.refill_per_second)
                if tokens < cost:
                    _local_buckets[key] = (tokens, now)
                    return ThrottleResult(False, max(1, int((cost - tokens) / self.refill_per_second + 0.999)))
                _local_buckets[key] = (tokens - cost, now)
                return ThrottleResult(True)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def consume_route_limit(request: Request, payload: dict | None, group: str) -> ThrottleResult:
    """按登录用户限流；游客退化为 IP。group 仅允许配置中的高成本入口。"""
    if not settings.RATE_LIMIT_USER_ENABLED:
        return ThrottleResult(True)
    spec = {
        "chat_stream": (settings.RATE_LIMIT_CHAT_STREAM_CAPACITY, settings.RATE_LIMIT_CHAT_STREAM_REFILL_PER_MINUTE),
        "office_submit": (settings.RATE_LIMIT_OFFICE_SUBMIT_CAPACITY, settings.RATE_LIMIT_OFFICE_SUBMIT_REFILL_PER_MINUTE),
        "upload": (settings.RATE_LIMIT_UPLOAD_CAPACITY, settings.RATE_LIMIT_UPLOAD_REFILL_PER_MINUTE),
    }.get(group)
    if spec is None:
        return ThrottleResult(True)
    identity = str((payload or {}).get("sub") or f"ip:{client_ip(request)}")
    return await TokenBucketLimiter(group, *spec).consume(identity)
