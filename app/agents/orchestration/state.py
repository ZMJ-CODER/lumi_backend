"""任务状态存储：Redis（生产，appendonly 持久化）+ InMemory（测试）.

多 agent 任务的中断/恢复/保留已完成任务都以这里的持久化状态为前提。
Redis 部署已开启 appendonly，容器重启后任务状态仍在。
"""

from abc import ABC, abstractmethod
import asyncio
import json

from loguru import logger
from redis.exceptions import WatchError

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.core.config import settings
from app.core.redis import get_redis


def _key(job_id: str) -> str:
    return f"multiagent:job:{job_id}"


class StateConflictError(RuntimeError):
    """保存的 revision 已落后于存储中的版本。"""


class StatePersistenceError(RuntimeError):
    """任务创建后无法从共享状态库读回，禁止把无效 job_id 发给前端。"""


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


def _repair_legacy_empty_arrays(value):
    """兼容 Redis Lua cjson 把空 Python list 编为 {} 的历史快照。

    Lua 的 cjson 无法区分空数组和空对象；任务状态中这些字段固定是数组，
    因此可安全地在读取时恢复。新保存路径不再经过 Lua 的 cjson 重编码。
    """
    if isinstance(value, list):
        return [_repair_legacy_empty_arrays(item) for item in value]
    if not isinstance(value, dict):
        return value
    repaired = {key: _repair_legacy_empty_arrays(item) for key, item in value.items()}
    for node in repaired.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        for field in ("depends_on", "resource_claims"):
            if node.get(field) == {}:
                node[field] = []
    return repaired


def _decode_job(raw: str, job_id: str) -> Job | None:
    """先走正常快速路径，失败后兼容修复旧任务 JSON。"""
    try:
        return Job.model_validate_json(raw)
    except Exception as original_exc:  # noqa: BLE001
        try:
            repaired = _repair_legacy_empty_arrays(json.loads(raw))
            job = Job.model_validate(repaired)
            logger.warning("任务状态已兼容修复空数组编码: {}", job_id)
            return job
        except Exception as repair_exc:  # noqa: BLE001
            logger.warning("任务状态反序列化失败 {}: {} | 修复失败: {}", job_id, original_exc, repair_exc)
            return None


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
        # 提交接口和 SSE 会立即把 job_id 交给另一条 HTTP 请求轮询。这里必须
        # 确认共享 Redis 已可读；否则不能让前端拿到一个必然 404 的任务 ID。
        raw = await r.get(_key(job.job_id))
        if not raw or _decode_job(raw, job.job_id) is None:
            raise StatePersistenceError(f"任务状态未能持久化: {job.job_id}")
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
        return _decode_job(raw, job_id)

    async def save_job(self, job: Job) -> None:
        lock = self._save_locks.setdefault(job.job_id, asyncio.Lock())
        async with lock:
            r = get_redis()
            key = _key(job.job_id)
            # 不用 Lua cjson decode/encode：它会把空数组 [] 写成 {}，导致下一次
            # Pydantic 读取 depends_on/resource_claims 失败，SSE 因而无法收敛。
            # 本进程锁覆盖正常 worker/API 写入；跨进程冲突通过 revision 检测后重试。
            candidate = job
            for _ in range(3):
                # WATCH/MULTI 在不经过 Lua cjson 的前提下保持跨进程 CAS。
                async with r.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        raw = await pipe.get(key)
                        current = _decode_job(raw, job.job_id) if raw else None
                        if current is None:
                            raise StateConflictError(f"任务不存在: {job.job_id}")
                        if current.revision != candidate.revision:
                            candidate = _merge_conflict(current, candidate)
                            continue
                        candidate.revision = current.revision + 1
                        pipe.multi()
                        pipe.set(key, candidate.model_dump_json(), ex=self._ttl)
                        await pipe.execute()
                        _update_job_in_place(job, candidate)
                        return
                    except WatchError:
                        # 其他进程刚好写入，重读后按 revision 合并再试。
                        continue
                    finally:
                        await pipe.reset()
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
