"""Backend-neutral admission leases with an optional Redis ZSET backend."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class AdmissionBackpressureError(RuntimeError):
    """Raised when admission cannot reserve a safe capacity slot."""


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    lease_seconds: int
    max_inflight: int
    max_active_jobs: int
    max_active_jobs_per_user: int


class AdmissionPort(Protocol):
    async def reserve(self, token: str) -> None: ...

    async def promote(self, token: str, job_id: str, user_id: str) -> None: ...

    async def activate(self, job_id: str, user_id: str) -> None: ...

    async def renew(self, job_id: str, user_id: str) -> bool: ...

    async def release(self, *, token: str | None = None, job_id: str | None = None, user_id: str | None = None) -> None: ...


RedisProvider = Callable[[], Awaitable[Any | None] | Any | None]
LimitsProvider = Callable[[], AdmissionLimits]


class JobAdmission:
    """Lease admission with Redis coordination and process-local fallback.

    The coordinator owns protocol semantics only. Application adapters supply
    Redis construction, policy-derived limits and localized error presentation.
    """

    _RESERVE_LUA = """
local now = tonumber(ARGV[1]); local until = tonumber(ARGV[2]); local max_inflight = tonumber(ARGV[3]); local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= max_inflight then return 0 end
redis.call('ZADD', KEYS[1], until, token)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""
    _PROMOTE_LUA = """
local now = tonumber(ARGV[1]); local until = tonumber(ARGV[2]); local global_limit = tonumber(ARGV[3]); local user_limit = tonumber(ARGV[4]); local token = ARGV[5]; local job_id = ARGV[6]
for i=1,3 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
redis.call('ZREM', KEYS[3], token)
if redis.call('ZCARD', KEYS[1]) >= global_limit then return 0 end
if redis.call('ZCARD', KEYS[2]) >= user_limit then return -1 end
redis.call('ZADD', KEYS[1], until, job_id)
redis.call('ZADD', KEYS[2], until, job_id)
for i=1,3 do redis.call('EXPIRE', KEYS[i], tonumber(ARGV[7])) end
return 1
"""
    _RENEW_LUA = """
local now = tonumber(ARGV[1]); local until = tonumber(ARGV[2]); local job_id = ARGV[3]; local ttl = tonumber(ARGV[4])
for i=1,2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if redis.call('ZSCORE', KEYS[1], job_id) == false then return 0 end
if redis.call('ZSCORE', KEYS[2], job_id) == false then return 0 end
redis.call('ZADD', KEYS[1], 'XX', until, job_id)
redis.call('ZADD', KEYS[2], 'XX', until, job_id)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""
    _ACTIVATE_LUA = """
local now = tonumber(ARGV[1]); local until = tonumber(ARGV[2]); local global_limit = tonumber(ARGV[3]); local user_limit = tonumber(ARGV[4]); local job_id = ARGV[5]; local ttl = tonumber(ARGV[6])
for i=1,2 do redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now) end
if redis.call('ZSCORE', KEYS[1], job_id) ~= false and redis.call('ZSCORE', KEYS[2], job_id) ~= false then
  redis.call('ZADD', KEYS[1], 'XX', until, job_id); redis.call('ZADD', KEYS[2], 'XX', until, job_id)
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= global_limit then return 0 end
if redis.call('ZCARD', KEYS[2]) >= user_limit then return -1 end
redis.call('ZADD', KEYS[1], until, job_id); redis.call('ZADD', KEYS[2], until, job_id)
redis.call('EXPIRE', KEYS[1], ttl); redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""

    def __init__(
        self,
        *,
        redis_provider: RedisProvider | None = None,
        limits_provider: LimitsProvider | None = None,
    ) -> None:
        self._redis_provider = redis_provider
        self._limits_provider = limits_provider or self._default_limits
        self._inflight: dict[str, float] = {}
        self._global: dict[str, float] = {}
        self._users: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_limits() -> AdmissionLimits:
        return AdmissionLimits(
            lease_seconds=7200,
            max_inflight=8,
            max_active_jobs=32,
            max_active_jobs_per_user=2,
        )

    def limits(self) -> AdmissionLimits:
        return self._limits_provider()

    @staticmethod
    def _keys(user_id: str) -> tuple[str, str, str]:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        return "agent:admission:active", f"agent:admission:user:{digest}", "agent:admission:inflight"

    @staticmethod
    def _purge(values: dict[str, float], now: float) -> None:
        for key, until in list(values.items()):
            if until <= now:
                values.pop(key, None)

    async def _redis(self) -> Any | None:
        if self._redis_provider is None:
            return None
        result = self._redis_provider()
        return await result if inspect.isawaitable(result) else result

    def _lease_seconds(self) -> int:
        return max(60, self.limits().lease_seconds)

    async def reserve(self, token: str) -> None:
        now = time.time()
        limits = self.limits()
        lease = self._lease_seconds()
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("admission backend unavailable")
            result = await redis.eval(
                self._RESERVE_LUA, 1, self._keys("")[2], now, now + lease, limits.max_inflight, token, lease
            )
            if int(result) != 1:
                raise AdmissionBackpressureError("admission submission capacity is full") from None
            return
        except AdmissionBackpressureError:
            raise
        except Exception:  # noqa: BLE001
            async with self._lock:
                self._purge(self._inflight, now)
                if len(self._inflight) >= limits.max_inflight:
                    raise AdmissionBackpressureError("admission submission capacity is full") from None
                self._inflight[token] = now + lease

    async def promote(self, token: str, job_id: str, user_id: str) -> None:
        now = time.time()
        limits = self.limits()
        lease = self._lease_seconds()
        global_key, user_key, inflight_key = self._keys(user_id)
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("admission backend unavailable")
            result = await redis.eval(
                self._PROMOTE_LUA,
                3,
                global_key,
                user_key,
                inflight_key,
                now,
                now + lease,
                limits.max_active_jobs,
                limits.max_active_jobs_per_user,
                token,
                job_id,
                lease,
            )
            if int(result) == 0:
                raise AdmissionBackpressureError("admission active capacity is full") from None
            if int(result) < 0:
                raise AdmissionBackpressureError("admission user active capacity is full") from None
            return
        except AdmissionBackpressureError:
            raise
        except Exception:  # noqa: BLE001
            async with self._lock:
                self._purge(self._inflight, now)
                self._purge(self._global, now)
                users = self._users.setdefault(user_id, {})
                self._purge(users, now)
                self._inflight.pop(token, None)
                if len(self._global) >= limits.max_active_jobs:
                    raise AdmissionBackpressureError("admission active capacity is full") from None
                if len(users) >= limits.max_active_jobs_per_user:
                    raise AdmissionBackpressureError("admission user active capacity is full") from None
                self._global[job_id] = now + lease
                users[job_id] = now + lease

    async def renew(self, job_id: str, user_id: str) -> bool:
        now = time.time()
        lease = self._lease_seconds()
        global_key, user_key, _ = self._keys(user_id)
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("admission backend unavailable")
            result = await redis.eval(self._RENEW_LUA, 2, global_key, user_key, now, now + lease, job_id, lease)
            return int(result or 0) == 1
        except Exception:  # noqa: BLE001
            async with self._lock:
                self._purge(self._global, now)
                users = self._users.setdefault(user_id, {})
                self._purge(users, now)
                if job_id not in self._global or job_id not in users:
                    return False
                self._global[job_id] = now + lease
                users[job_id] = now + lease
                return True

    async def activate(self, job_id: str, user_id: str) -> None:
        """Acquire a fresh active slot when a suspended job is resumed."""
        now = time.time()
        limits = self.limits()
        lease = self._lease_seconds()
        global_key, user_key, _ = self._keys(user_id)
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("admission backend unavailable")
            result = await redis.eval(
                self._ACTIVATE_LUA, 2, global_key, user_key, now, now + lease,
                limits.max_active_jobs, limits.max_active_jobs_per_user, job_id, lease,
            )
            if int(result) == 0:
                raise AdmissionBackpressureError("admission active capacity is full") from None
            if int(result) < 0:
                raise AdmissionBackpressureError("admission user active capacity is full") from None
            return
        except AdmissionBackpressureError:
            raise
        except Exception:  # noqa: BLE001
            async with self._lock:
                self._purge(self._global, now)
                users = self._users.setdefault(user_id, {})
                self._purge(users, now)
                if job_id not in self._global and len(self._global) >= limits.max_active_jobs:
                    raise AdmissionBackpressureError("admission active capacity is full") from None
                if job_id not in users and len(users) >= limits.max_active_jobs_per_user:
                    raise AdmissionBackpressureError("admission user active capacity is full") from None
                self._global[job_id] = now + lease
                users[job_id] = now + lease

    async def release(
        self,
        *,
        token: str | None = None,
        job_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if not token and not job_id:
            return
        try:
            redis = await self._redis()
            if redis is None:
                raise RuntimeError("admission backend unavailable")
            if token:
                await redis.zrem(self._keys("")[2], token)
            if job_id:
                global_key, user_key, _ = self._keys(user_id or "")
                await redis.zrem(global_key, job_id)
                if user_id:
                    await redis.zrem(user_key, job_id)
            return
        except Exception:  # noqa: BLE001
            async with self._lock:
                if token:
                    self._inflight.pop(token, None)
                if job_id:
                    self._global.pop(job_id, None)
                    if user_id:
                        self._users.get(user_id, {}).pop(job_id, None)
