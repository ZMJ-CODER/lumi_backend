"""任务状态存储：Redis（生产，appendonly 持久化）+ InMemory（测试）.

多 agent 任务的中断/恢复/保留已完成任务都以这里的持久化状态为前提。
Redis 部署已开启 appendonly，容器重启后任务状态仍在。
"""

import json
from abc import ABC, abstractmethod

from loguru import logger

from app.agents.orchestration.models import Job
from app.core.config import settings
from app.core.redis import get_redis


def _key(job_id: str) -> str:
    return f"multiagent:job:{job_id}"


class StateStore(ABC):
    """任务状态存取接口（Redis / InMemory 两种实现可互换）."""

    @abstractmethod
    async def create_job(self, job: Job) -> None: ...

    @abstractmethod
    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> Job | None: ...

    @abstractmethod
    async def save_job(self, job: Job) -> None: ...

    @abstractmethod
    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]: ...

    @abstractmethod
    async def delete_job(self, job_id: str) -> None: ...


class RedisStateStore(StateStore):
    """Redis 实现：整个 Job（含任务树）存为一条 JSON，天然原子."""

    def __init__(self, ttl_seconds: int | None = None):
        self._ttl = ttl_seconds or settings.AGENT_JOBS_TTL_SECONDS

    async def create_job(self, job: Job) -> None:
        r = get_redis()
        await r.set(_key(job.job_id), job.model_dump_json(), ex=self._ttl)
        # 用户任务索引：幂等写入（同一 job 不重复），供 list_jobs 倒序分页
        index_key = f"multiagent:user_jobs:{job.user_id}"
        await r.lrem(index_key, 0, job.job_id)
        await r.lpush(index_key, job.job_id)
        await r.ltrim(index_key, 0, 999)
        await r.expire(index_key, self._ttl)

    async def get_job(self, job_id: str) -> Job | None:
        r = get_redis()
        raw = await r.get(_key(job_id))
        if not raw:
            return None
        try:
            return Job.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("任务状态反序列化失败 {}: {}", job_id, exc)
            return None

    async def save_job(self, job: Job) -> None:
        await self.create_job(job)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        ids = await self.list_job_ids(user_id, limit)
        jobs: list[Job] = []
        for jid in ids:
            job = await self.get_job(jid)
            if job:
                jobs.append(job)
        return jobs

    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]:
        r = get_redis()
        # 按用户维护的 job_id 列表（create_job 时写入）；数据量大后再分页
        key = f"multiagent:user_jobs:{user_id}"
        return [str(x) for x in await r.lrange(key, 0, limit - 1)]

    async def delete_job(self, job_id: str) -> None:
        r = get_redis()
        await r.delete(_key(job_id))


class InMemoryStateStore(StateStore):
    """内存实现（测试/单进程调试用），行为与 Redis 版一致."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._user_index: dict[str, list[str]] = {}

    async def create_job(self, job: Job) -> None:
        self._jobs[job.job_id] = job.model_copy(deep=True)
        self._user_index.setdefault(job.user_id, []).insert(0, job.job_id)

    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]:
        return list(self._user_index.get(user_id, [])[:limit])

    async def get_job(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        return job.model_copy(deep=True) if job else None

    async def save_job(self, job: Job) -> None:
        self._jobs[job.job_id] = job.model_copy(deep=True)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        ids = await self.list_job_ids(user_id, limit)
        return [self._jobs[i] for i in ids if i in self._jobs]

    async def delete_job(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job:
            ids = self._user_index.get(job.user_id, [])
            if job_id in ids:
                ids.remove(job_id)
