"""Resource-claim coordination with pluggable distributed storage."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import uuid
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ResourceClaim(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    mode: Literal["read", "write"] = "read"


class ResourceCoordinatorPort(Protocol):
    async def write_coordination_available(self, claims: list[ResourceClaim]) -> bool: ...


class WriteResourceCoordinationUnavailable(RuntimeError):
    """A write must not proceed without a distributed ownership lease."""


RedisProvider = Callable[[], Awaitable[Any | None] | Any | None]
FailClosedPolicy = Callable[[ResourceClaim], bool]


_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local mode = ARGV[1]
local owner = ARGV[2]
local ttl = tonumber(ARGV[3])
local now = tonumber(redis.call('TIME')[1])
local writer = redis.call('HGET', key, 'writer')
local writer_until = tonumber(redis.call('HGET', key, 'writer_until') or '0')
if writer and writer_until > 0 and writer_until <= now then
  redis.call('HDEL', key, 'writer', 'writer_until')
end
local entries = redis.call('HGETALL', key)
for i = 1, #entries, 2 do
  local field = entries[i]
  local expires = tonumber(entries[i + 1] or '0')
  if string.sub(field, 1, 7) == 'reader:' and expires > 0 and expires <= now then
    redis.call('HDEL', key, field)
  end
end
if mode == 'read' then
  if redis.call('HEXISTS', key, 'writer') == 1 then return 0 end
  redis.call('HSET', key, 'reader:' .. owner, now + ttl)
else
  if redis.call('HLEN', key) ~= 0 then return 0 end
  redis.call('HSET', key, 'writer', owner, 'writer_until', now + ttl)
end
redis.call('EXPIRE', key, ttl)
return 1
"""

_RELEASE_SCRIPT = """
local key = KEYS[1]
local mode = ARGV[1]
local owner = ARGV[2]
if mode == 'read' then
  redis.call('HDEL', key, 'reader:' .. owner)
elseif redis.call('HGET', key, 'writer') == owner then
  redis.call('HDEL', key, 'writer', 'writer_until')
end
if redis.call('HLEN', key) == 0 then redis.call('DEL', key) end
return 1
"""

_RENEW_SCRIPT = """
local key = KEYS[1]
local mode = ARGV[1]
local owner = ARGV[2]
local ttl = tonumber(ARGV[3])
local now = tonumber(redis.call('TIME')[1])
if mode == 'read' then
  if redis.call('HEXISTS', key, 'reader:' .. owner) ~= 1 then return 0 end
  redis.call('HSET', key, 'reader:' .. owner, now + ttl)
elseif redis.call('HGET', key, 'writer') ~= owner then
  return 0
else
  redis.call('HSET', key, 'writer_until', now + ttl)
end
redis.call('EXPIRE', key, ttl)
return 1
"""


class ResourceCoordinator:
    """Coordinates claims with injected storage and fail-closed policy."""

    def __init__(
        self,
        *,
        redis_provider: RedisProvider | None = None,
        fail_closed: FailClosedPolicy | None = None,
    ) -> None:
        self._redis_provider = redis_provider
        self._fail_closed = fail_closed or (lambda _claim: False)
        self._conditions: weakref.WeakKeyDictionary[Any, asyncio.Condition] = weakref.WeakKeyDictionary()
        self._locals: weakref.WeakKeyDictionary[Any, dict[str, dict[str, int | bool]]] = weakref.WeakKeyDictionary()

    def _local_state(self) -> tuple[asyncio.Condition, dict[str, dict[str, int | bool]]]:
        loop = asyncio.get_running_loop()
        condition = self._conditions.get(loop)
        if condition is None:
            condition = asyncio.Condition()
            self._conditions[loop] = condition
            self._locals[loop] = {}
        return condition, self._locals[loop]

    @staticmethod
    def _redis_key(resource: str) -> str:
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
        return f"agent:resource:{digest}"

    async def _redis(self) -> Any | None:
        if self._redis_provider is None:
            return None
        result = self._redis_provider()
        return await result if inspect.isawaitable(result) else result

    def _requires_fail_closed(self, claim: ResourceClaim) -> bool:
        return claim.mode == "write" and self._fail_closed(claim)

    async def write_coordination_available(self, claims: list[ResourceClaim]) -> bool:
        if not any(self._requires_fail_closed(claim) for claim in claims):
            return True
        try:
            redis = await self._redis()
            if redis is None:
                return False
            await redis.ping()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _acquire_one(self, claim: ResourceClaim, owner: str, ttl: int) -> bool:
        try:
            redis = await self._redis()
            if redis is not None:
                key = self._redis_key(claim.key)
                while not await redis.eval(_ACQUIRE_SCRIPT, 1, key, claim.mode, owner, ttl):
                    await asyncio.sleep(0.05)
                return True
        except Exception as exc:  # noqa: BLE001
            if self._requires_fail_closed(claim):
                raise WriteResourceCoordinationUnavailable("write resource coordination is unavailable") from exc
        if self._requires_fail_closed(claim):
            raise WriteResourceCoordinationUnavailable("write resource coordination is unavailable")
        condition, local = self._local_state()
        async with condition:
            while True:
                state = local.setdefault(claim.key, {"readers": 0, "writer": False})
                available = not bool(state["writer"]) and (claim.mode == "read" or int(state["readers"]) == 0)
                if available:
                    if claim.mode == "read":
                        state["readers"] = int(state["readers"]) + 1
                    else:
                        state["writer"] = True
                    return False
                await condition.wait()

    async def _renew_one(self, claim: ResourceClaim, owner: str, ttl: int) -> bool:
        redis = await self._redis()
        if redis is None:
            return False
        result = await redis.eval(_RENEW_SCRIPT, 1, self._redis_key(claim.key), claim.mode, owner, ttl)
        return int(result or 0) == 1

    async def _release_one(self, claim: ResourceClaim, owner: str, *, uses_redis: bool) -> None:
        if uses_redis:
            redis = await self._redis()
            if redis is not None:
                await redis.eval(_RELEASE_SCRIPT, 1, self._redis_key(claim.key), claim.mode, owner)
                return
        condition, local = self._local_state()
        async with condition:
            state = local.get(claim.key)
            if state:
                if claim.mode == "read":
                    state["readers"] = max(0, int(state["readers"]) - 1)
                else:
                    state["writer"] = False
                if not state["writer"] and int(state["readers"]) == 0:
                    local.pop(claim.key, None)
            condition.notify_all()

    @asynccontextmanager
    async def claim(self, claims: list[ResourceClaim], ttl: int = 360):
        owner = uuid.uuid4().hex
        ttl = max(3, int(ttl))
        acquired: list[tuple[ResourceClaim, bool]] = []
        renew_task: asyncio.Task[None] | None = None
        holder = asyncio.current_task()

        async def renew_loop(redis_claims: list[ResourceClaim]) -> None:
            interval = max(1.0, min(30.0, ttl / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = [await self._renew_one(claim, owner, ttl) for claim in redis_claims]
                except Exception:  # noqa: BLE001
                    renewed = [False]
                if not all(renewed):
                    if holder is not None and not holder.done():
                        holder.cancel()
                    return

        try:
            for claim in sorted(claims, key=lambda item: item.key):
                acquired.append((claim, await self._acquire_one(claim, owner, ttl)))
            redis_claims = [claim for claim, uses_redis in acquired if uses_redis]
            if redis_claims:
                renew_task = asyncio.create_task(renew_loop(redis_claims))
            yield
        finally:
            if renew_task is not None:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
            for claim, uses_redis in reversed(acquired):
                await self._release_one(claim, owner, uses_redis=uses_redis)
