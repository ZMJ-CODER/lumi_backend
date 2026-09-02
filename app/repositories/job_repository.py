"""任务的持久化边界。

首个实现围绕既有 ``StateStore`` 建立适配器。保持适配器精简，使编排代码
依赖仓储契约，同时在迁移期间不改变 Redis/InMemory 的既有行为。
"""

from __future__ import annotations

from typing import Protocol

from app.agents.orchestration.models import Job
from app.agents.orchestration.state import StateStore


class JobRepository(Protocol):
    async def create_job(self, job: Job) -> None: ...

    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]: ...

    async def list_all_job_ids(self, limit: int = 50) -> list[str]: ...

    async def get_job(self, job_id: str) -> Job | None: ...

    async def save_job(self, job: Job) -> None: ...

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]: ...

    async def delete_job(self, job_id: str) -> None: ...


class StateStoreJobRepository:
    """Repository adapter preserving the current state store semantics."""

    def __init__(self, store: StateStore):
        self._store = store

    async def create_job(self, job: Job) -> None:
        await self._store.create_job(job)

    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]:
        return await self._store.list_job_ids(user_id, limit)

    async def list_all_job_ids(self, limit: int = 50) -> list[str]:
        # ``list_all_job_ids`` is implemented by the production and in-memory
        # stores.  Keep a defensive fallback for third-party StateStore plugs.
        method = getattr(self._store, "list_all_job_ids", None)
        if method is None:
            return []
        return await method(limit)

    async def get_job(self, job_id: str) -> Job | None:
        return await self._store.get_job(job_id)

    async def save_job(self, job: Job) -> None:
        await self._store.save_job(job)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        return await self._store.list_jobs(user_id, limit)

    async def delete_job(self, job_id: str) -> None:
        await self._store.delete_job(job_id)
