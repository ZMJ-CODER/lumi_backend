"""办公任务的跨进程准入控制：短期规划 reservation + 活跃任务容量。"""

from __future__ import annotations

import asyncio
import time

from app.core.config import settings


class AdmissionBackpressureError(RuntimeError):
    pass


class JobAdmission:
    """Redis ZSET 作为带租约的并发槽位，Redis 不可用时回退本进程实现。"""

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

    def __init__(self) -> None:
        self._inflight: dict[str, float] = {}
        self._global: dict[str, float] = {}
        self._users: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _keys(user_id: str) -> tuple[str, str, str]:
        # user_id 可来自 JWT，不直接拼入键以避免 key injection 与敏感标识暴露。
        import hashlib

        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        return "agent:admission:active", f"agent:admission:user:{digest}", "agent:admission:inflight"

    @staticmethod
    def _purge(values: dict[str, float], now: float) -> None:
        for key, until in list(values.items()):
            if until <= now:
                values.pop(key, None)

    async def reserve(self, token: str) -> None:
        now = time.time()
        lease = max(60, settings.AGENT_ADMISSION_LEASE_SECONDS)
        try:
            from app.core.redis import get_redis

            key = self._keys("")[2]
            result = await get_redis().eval(
                self._RESERVE_LUA, 1, key, now, now + lease, settings.AGENT_SUBMISSION_MAX_INFLIGHT, token, lease
            )
            if int(result) != 1:
                raise AdmissionBackpressureError("办公任务正在繁忙处理，请稍后重试或切换普通模式对话")
            return
        except AdmissionBackpressureError:
            raise
        except Exception:
            async with self._lock:
                self._purge(self._inflight, now)
                if len(self._inflight) >= settings.AGENT_SUBMISSION_MAX_INFLIGHT:
                    raise AdmissionBackpressureError("办公任务正在繁忙处理，请稍后重试或切换普通模式对话")
                self._inflight[token] = now + lease

    async def promote(self, token: str, job_id: str, user_id: str) -> None:
        now = time.time()
        lease = max(60, settings.AGENT_ADMISSION_LEASE_SECONDS)
        global_key, user_key, inflight_key = self._keys(user_id)
        try:
            from app.core.redis import get_redis

            result = await get_redis().eval(
                self._PROMOTE_LUA, 3, global_key, user_key, inflight_key, now, now + lease,
                settings.AGENT_GLOBAL_ACTIVE_JOB_LIMIT, settings.AGENT_USER_ACTIVE_JOB_LIMIT, token, job_id, lease,
            )
            if int(result) == 0:
                raise AdmissionBackpressureError("办公任务容量已满，请稍后重试或切换普通模式对话")
            if int(result) < 0:
                raise AdmissionBackpressureError("当前有任务正在进行中，请切换到普通模式对话")
            return
        except AdmissionBackpressureError:
            raise
        except Exception:
            async with self._lock:
                self._purge(self._inflight, now)
                self._purge(self._global, now)
                users = self._users.setdefault(user_id, {})
                self._purge(users, now)
                self._inflight.pop(token, None)
                if len(self._global) >= settings.AGENT_GLOBAL_ACTIVE_JOB_LIMIT:
                    raise AdmissionBackpressureError("办公任务容量已满，请稍后重试或切换普通模式对话")
                if len(users) >= settings.AGENT_USER_ACTIVE_JOB_LIMIT:
                    raise AdmissionBackpressureError("当前有任务正在进行中，请切换到普通模式对话")
                self._global[job_id] = now + lease
                users[job_id] = now + lease

    async def renew(self, job_id: str, user_id: str) -> bool:
        """Extend an existing active slot without creating a missing lease."""
        now = time.time()
        lease = max(60, settings.AGENT_ADMISSION_LEASE_SECONDS)
        global_key, user_key, _ = self._keys(user_id)
        try:
            from app.core.redis import get_redis

            result = await get_redis().eval(
                self._RENEW_LUA,
                2,
                global_key,
                user_key,
                now,
                now + lease,
                job_id,
                lease,
            )
            return int(result or 0) == 1
        except Exception:
            async with self._lock:
                self._purge(self._global, now)
                users = self._users.setdefault(user_id, {})
                self._purge(users, now)
                if job_id not in self._global or job_id not in users:
                    return False
                self._global[job_id] = now + lease
                users[job_id] = now + lease
                return True

    async def release(self, *, token: str | None = None, job_id: str | None = None, user_id: str | None = None) -> None:
        if not token and not job_id:
            return
        try:
            from app.core.redis import get_redis

            if token:
                await get_redis().zrem(self._keys("")[2], token)
            if job_id:
                global_key, user_key, _ = self._keys(user_id or "")
                await get_redis().zrem(global_key, job_id)
                if user_id:
                    await get_redis().zrem(user_key, job_id)
            return
        except Exception:
            async with self._lock:
                if token:
                    self._inflight.pop(token, None)
                if job_id:
                    self._global.pop(job_id, None)
                    if user_id:
                        self._users.get(user_id, {}).pop(job_id, None)


job_admission = JobAdmission()
