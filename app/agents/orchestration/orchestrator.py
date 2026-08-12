"""多智能体编排器 —— 提交任务 → 规划 → DAG 执行 → 状态管理.

对外能力：
  - submit_job: 规划任务树并后台执行，立即返回 job
  - get_job / list_jobs: 查询状态（前端任务面板数据源）
  - cancel_job: 用户终止（可选择保留已完成节点）
  - pause_job / resume_job: 暂停/恢复调度

断网/超时策略：节点执行超时（默认 5 分钟）→ 重试 → 失败；
任务整体可通过 cancel 中断，恢复由状态存储保障（Redis appendonly 持久化）。
"""

import asyncio
import time
import uuid

from loguru import logger

from app.agents.orchestration.dag import DagValidationError, execute_dag
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.planner import Planner, RulePlanner
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.state import RedisStateStore, StateStore
from app.agents.orchestration.workers import WORKERS


class AgentOrchestrator:
    """多智能体协作编排器（单例，全局复用）."""

    def __init__(
        self,
        store: StateStore | None = None,
        planner: Planner | None = None,
        workers: dict | None = None,
        review: ReviewHook | None = None,
    ):
        self._store = store or RedisStateStore()
        self._planner = planner or RulePlanner()
        self._workers = workers if workers is not None else WORKERS
        self._review = review or get_reviewer()
        self._tasks: dict[str, asyncio.Task] = {}  # job_id -> 后台执行任务

    # ── 提交与执行 ──────────────────────────────────

    async def submit_job(self, user_id: str, request: str, scene: str = "office") -> Job:
        """规划任务树并启动后台执行，立即返回 Job."""
        tree = await self._planner.plan(user_id, request, scene)
        job = Job(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            request=request,
            scene=scene,
            status=JobStatus.RUNNING,
            nodes=tree.nodes,
        )
        await self._store.create_job(job)
        task = asyncio.create_task(self._run_job(job.job_id))
        self._tasks[job.job_id] = task
        logger.info("多智能体任务已提交: {} | agent={} request={}", job.job_id[:8], [n.agent for n in job.nodes], request[:40])
        return job

    async def _run_job(self, job_id: str) -> None:
        """后台执行：校验 DAG → 拓扑执行 → 汇总状态."""
        try:
            job = await self._store.get_job(job_id)
            if job is None:
                return
            await execute_dag(job, self._workers, self._review, self._store)
        except DagValidationError as exc:
            logger.error("任务 DAG 非法 {}: {}", job_id, exc)
            job = await self._store.get_job(job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.updated_at = time.time()
                await self._store.save_job(job)
        except asyncio.CancelledError:
            logger.info("任务后台执行被取消: {}", job_id)
            job = await self._store.get_job(job_id)
            if job and job.status not in (JobStatus.CANCELLED, JobStatus.INTERRUPTED):
                job.status = JobStatus.INTERRUPTED
                job.updated_at = time.time()
                await self._store.save_job(job)
        except Exception as exc:  # noqa: BLE001
            logger.error("任务执行异常 {}: {}", job_id, exc)
            job = await self._store.get_job(job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.updated_at = time.time()
                await self._store.save_job(job)
        finally:
            self._tasks.pop(job_id, None)

    # ── 查询 ────────────────────────────────────────

    async def get_job(self, job_id: str) -> Job | None:
        return await self._store.get_job(job_id)

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        return await self._store.list_jobs(user_id, limit)

    # ── 控制：终止 / 暂停 / 恢复 ─────────────────────

    async def cancel_job(self, job_id: str, keep_completed: bool = True) -> Job | None:
        """终止任务：设置 CANCELLED 状态，执行循环会在 1 秒内停止调度并中断运行节点.

        不直接 cancel 后台 asyncio task（会留下孤儿节点任务）；
        改为状态驱动：execute_dag 轮询到 CANCELLED 后自行收尾。
        """
        job = await self._store.get_job(job_id)
        if job is None or job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            return job
        job.status = JobStatus.CANCELLED
        job.updated_at = time.time()
        if not keep_completed:
            for n in job.nodes:
                if n.status in (
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.RETRYING,
                ):
                    n.status = TaskStatus.CANCELLED
                    n.error = "任务被用户终止"
        await self._store.save_job(job)
        return job

    async def pause_job(self, job_id: str) -> Job | None:
        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return job
        job.status = JobStatus.PAUSED
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job

    async def resume_job(self, job_id: str) -> Job | None:
        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.PAUSED:
            return job
        job.status = JobStatus.RUNNING
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
