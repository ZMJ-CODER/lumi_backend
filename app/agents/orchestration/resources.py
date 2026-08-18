"""DAG 外部资源读写锁；Redis 跨进程协调，失效时回退进程内协调。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
import weakref
from contextlib import asynccontextmanager

from app.agents.orchestration.models import ResourceClaim


_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local mode = ARGV[1]
local owner = ARGV[2]
local ttl = tonumber(ARGV[3])
if mode == 'read' then
  if redis.call('HEXISTS', key, 'writer') == 1 then return 0 end
  redis.call('HSET', key, 'reader:' .. owner, '1')
else
  if redis.call('HLEN', key) ~= 0 then return 0 end
  redis.call('HSET', key, 'writer', owner)
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
  redis.call('HDEL', key, 'writer')
end
if redis.call('HLEN', key) == 0 then redis.call('DEL', key) end
return 1
"""


class ResourceCoordinator:
    def __init__(self) -> None:
        self._conditions: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._locals: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _local_state(self):
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

    async def _redis(self):
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    async def _acquire_one(self, claim: ResourceClaim, owner: str, ttl: int) -> None:
        redis = await self._redis()
        if redis is not None:
            key = self._redis_key(claim.key)
            while not await redis.eval(_ACQUIRE_SCRIPT, 1, key, claim.mode, owner, ttl):
                await asyncio.sleep(0.05)
            return
        condition, local = self._local_state()
        async with condition:
            while True:
                state = local.setdefault(claim.key, {"readers": 0, "writer": False})
                available = not state["writer"] and (claim.mode == "read" or state["readers"] == 0)
                if available:
                    if claim.mode == "read":
                        state["readers"] = int(state["readers"]) + 1
                    else:
                        state["writer"] = True
                    return
                await condition.wait()

    async def _release_one(self, claim: ResourceClaim, owner: str) -> None:
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
                if not state["writer"] and state["readers"] == 0:
                    local.pop(claim.key, None)
            condition.notify_all()

    @asynccontextmanager
    async def claim(self, claims: list[ResourceClaim], ttl: int = 360) -> None:
        owner = uuid.uuid4().hex
        acquired: list[ResourceClaim] = []
        try:
            for claim in sorted(claims, key=lambda c: c.key):
                await self._acquire_one(claim, owner, ttl)
                acquired.append(claim)
            yield
        finally:
            for claim in reversed(acquired):
                await self._release_one(claim, owner)


# legacy 单进程内跨 Job 共用；多进程/Temporal 由 Redis 锁提供全局协调。
resource_coordinator = ResourceCoordinator()
