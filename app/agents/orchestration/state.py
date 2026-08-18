"""任务状态存储：Redis（生产，appendonly 持久化）+ InMemory（测试）.

多 agent 任务的中断/恢复/保留已完成任务都以这里的持久化状态为前提。
Redis 部署已开启 appendonly，容器重启后任务状态仍在。
"""

from abc import ABC, abstractmethod
import asyncio

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.core.config import settings
from app.core.redis import get_redis


def _key(job_id: str) -> str:
    return f"multiagent:job:{job_id}"


class StateConflictError(RuntimeError):
    """保存的 revision 已落后于存储中的版本。"""


def _merge_conflict(current: Job, incoming: Job) -> Job:
    """冲突时合并控制面状态与节点进度，避免取消/暂停被旧快照覆盖。"""
    merged = incoming.model_copy(deep=True)
    control_statuses = {JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED}
    if current.status in control_statuses and incoming.status not in control_statuses:
        merged.status = current.status
    current_nodes = {node.id: node for node in current.nodes}
    protected = {TaskStatus.CANCELLED, TaskStatus.INTERRUPTED}
    for idx, node in enumerate(merged.nodes):
        saved = current_nodes.get(node.id)
        if saved is None:
            continue
        if current.status in {JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
            if saved.status == TaskStatus.RUNNING and node.status in {
                TaskStatus.INTERRUPTED,
                TaskStatus.CANCELLED,
            }:
                continue
            if saved.status in protected or node.status not in protected:
                merged.nodes[idx] = saved.model_copy(deep=True)
                continue
        if saved.status in protected and node.status not in protected:
            merged.nodes[idx] = saved.model_copy(deep=True)
    merged.revision = current.revision
    merged.updated_at = max(current.updated_at, incoming.updated_at)
    return merged


def _update_job_in_place(target: Job, source: Job) -> None:
    """把合并结果写回原 Job，同时保留调度器持有的 TaskNode 对象引用。"""
    target_nodes = {node.id: node for node in target.nodes}
    merged_nodes = []
    for source_node in source.nodes:
        target_node = target_nodes.get(source_node.id)
        if target_node is None:
            merged_nodes.append(source_node.model_copy(deep=True))
            continue
        for field_name in source_node.__class__.model_fields:
            setattr(target_node, field_name, getattr(source_node, field_name))
        merged_nodes.append(target_node)
    for field_name in target.__class__.model_fields:
        if field_name != "nodes":
            setattr(target, field_name, getattr(source, field_name))
    target.nodes = merged_nodes


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
        self._save_locks: dict[str, asyncio.Lock] = {}

    async def create_job(self, job: Job) -> None:
        r = get_redis()
        await r.set(_key(job.job_id), job.model_dump_json(), ex=self._ttl)
        # 用户任务索引：幂等写入（同一 job 不重复），供 list_jobs 倒序分页
        index_key = f"multiagent:user_jobs:{job.user_id}"
        await r.lrem(index_key, 0, job.job_id)
        await r.lpush(index_key, job.job_id)
        await r.ltrim(index_key, 0, 999)
        await r.expire(index_key, self._ttl)
        # 全量任务索引（管理后台跨用户查看）
        all_key = "multiagent:all_jobs"
        await r.lrem(all_key, 0, job.job_id)
        await r.lpush(all_key, job.job_id)
        await r.ltrim(all_key, 0, 4999)
        await r.expire(all_key, self._ttl)

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
        lock = self._save_locks.setdefault(job.job_id, asyncio.Lock())
        async with lock:
            r = get_redis()
            key = _key(job.job_id)
            script = """
local key = KEYS[1]
local expected = tonumber(ARGV[1])
local value = ARGV[2]
local ttl = tonumber(ARGV[3])
local raw = redis.call('GET', key)
if not raw then return -1 end
local current = cjson.decode(raw)
if tonumber(current['revision'] or 0) ~= expected then return 0 end
local updated = cjson.decode(value)
updated['revision'] = expected + 1
redis.call('SET', key, cjson.encode(updated), 'EX', ttl)
return expected + 1
"""
            candidate = job
            for _ in range(3):
                new_revision = int(
                    await r.eval(
                        script,
                        1,
                        key,
                        int(candidate.revision),
                        candidate.model_dump_json(),
                        self._ttl,
                    )
                )
                if new_revision < 0:
                    raise StateConflictError(f"任务不存在: {job.job_id}")
                if new_revision > 0:
                    candidate.revision = new_revision
                    _update_job_in_place(job, candidate)
                    return
                current = await self.get_job(job.job_id)
                if current is None:
                    raise StateConflictError(f"任务不存在: {job.job_id}")
                candidate = _merge_conflict(current, candidate)
            raise StateConflictError(f"任务状态版本冲突: {job.job_id}")

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

    async def list_all_job_ids(self, limit: int = 50) -> list[str]:
        r = get_redis()
        return [str(x) for x in await r.lrange("multiagent:all_jobs", 0, limit - 1)]

    async def delete_job(self, job_id: str) -> None:
        r = get_redis()
        await r.delete(_key(job_id))


class InMemoryStateStore(StateStore):
    """内存实现（测试/单进程调试用），行为与 Redis 版一致."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._user_index: dict[str, list[str]] = {}
        self._all_index: list[str] = []
        self._lock = asyncio.Lock()

    async def create_job(self, job: Job) -> None:
        self._jobs[job.job_id] = job.model_copy(deep=True)
        self._user_index.setdefault(job.user_id, []).insert(0, job.job_id)
        if job.job_id in self._all_index:
            self._all_index.remove(job.job_id)
        self._all_index.insert(0, job.job_id)

    async def list_job_ids(self, user_id: str, limit: int = 20) -> list[str]:
        return list(self._user_index.get(user_id, [])[:limit])

    async def list_all_job_ids(self, limit: int = 50) -> list[str]:
        return list(self._all_index[:limit])

    async def get_job(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        return job.model_copy(deep=True) if job else None

    async def save_job(self, job: Job) -> None:
        async with self._lock:
            current = self._jobs.get(job.job_id)
            if current is None:
                raise StateConflictError(f"任务不存在: {job.job_id}")
            candidate = job if current.revision == job.revision else _merge_conflict(current, job)
            candidate.revision = current.revision + 1
            _update_job_in_place(job, candidate)
            self._jobs[job.job_id] = candidate.model_copy(deep=True)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        ids = await self.list_job_ids(user_id, limit)
        return [self._jobs[i] for i in ids if i in self._jobs]

    async def delete_job(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job:
            ids = self._user_index.get(job.user_id, [])
            if job_id in ids:
                ids.remove(job_id)
