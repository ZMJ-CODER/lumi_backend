"""基于 Redis 的短租约，用于跨进程串行化计划补丁。"""

from __future__ import annotations

import asyncio
import secrets

from lumi_orch import PlanPatchConflict


class PlanPatchLock:
    """Combine an in-process mutex with an optional Redis compare-and-delete lease."""

    def __init__(self, job_id: str, *, timeout_seconds: float = 5.0, lease_seconds: int = 30) -> None:
        self._job_id = job_id
        self._timeout_seconds = timeout_seconds
        self._lease_seconds = lease_seconds
        self._local = asyncio.Lock()
        self._redis = None
        self._redis_key = f"multiagent:plan-patch-lock:{job_id}"
        self._token = ""

    async def __aenter__(self) -> "PlanPatchLock":
        try:
            await asyncio.wait_for(self._local.acquire(), timeout=self._timeout_seconds)
        except TimeoutError as exc:
            raise PlanPatchConflict("计划补图正在处理中，请稍后重试") from exc
        try:
            await self._acquire_redis_lease()
        except BaseException:
            self._local.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self._release_redis_lease()
        finally:
            self._local.release()

    async def _acquire_redis_lease(self) -> None:
        # Tests and narrow local tools deliberately run without init_redis().
        # Production state is Redis-backed, so an initialized-but-unavailable
        # Redis must fail closed instead of allowing two processes to append.
        from app.core import redis as redis_module

        if redis_module.redis_client is None:
            return
        self._redis = redis_module.get_redis()
        self._token = secrets.token_urlsafe(24)
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        while True:
            try:
                acquired = await self._redis.set(
                    self._redis_key,
                    self._token,
                    nx=True,
                    ex=self._lease_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                raise PlanPatchConflict("计划补图互斥锁不可用，拒绝并发写入") from exc
            if acquired:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise PlanPatchConflict("计划补图正在被其他进程处理，请稍后重试")
            await asyncio.sleep(0.05)

    async def _release_redis_lease(self) -> None:
        if self._redis is None or not self._token:
            return
        try:
            await self._redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                self._redis_key,
                self._token,
            )
        except Exception:
            # The lease has a short TTL.  Do not hide the original patch error
            # merely because cleanup lost a network race.
            pass
