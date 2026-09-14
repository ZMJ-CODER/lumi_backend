"""Plan-patch locking remains local for unit tests and distributed for Redis deployments."""

from __future__ import annotations

import asyncio

import pytest

from lumi_orch import PlanPatchConflict

from app.agents.orchestration.scheduling.locking import PlanPatchLock


def test_lock_uses_local_mutex_when_redis_is_not_initialized(monkeypatch):
    from app.core import redis as redis_module

    async def scenario():
        monkeypatch.setattr(redis_module, "redis_client", None)
        lock = PlanPatchLock("job-1", timeout_seconds=0.05)
        async with lock:
            with pytest.raises(PlanPatchConflict, match="正在处理中"):
                async with lock:
                    pass

    asyncio.run(scenario())


def test_initialized_redis_failure_rejects_instead_of_falling_back(monkeypatch):
    from app.core import redis as redis_module

    class BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("down")

    async def scenario():
        monkeypatch.setattr(redis_module, "redis_client", object())
        monkeypatch.setattr(redis_module, "get_redis", lambda: BrokenRedis())
        with pytest.raises(PlanPatchConflict, match="互斥锁不可用"):
            async with PlanPatchLock("job-1", timeout_seconds=0.05):
                pass

    asyncio.run(scenario())
