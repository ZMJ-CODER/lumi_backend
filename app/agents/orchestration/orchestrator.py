"""多智能体编排器 —— 提交任务 → 规划 → Temporal Workflow 执行 → 状态查询.

对外能力（API 契约不变）：
  - submit_job: 规划任务树 → 启动 Temporal Workflow（失败自动回退自建 DAG）
  - get_job / list_jobs: 查询状态（优先查询 Temporal，回退 Redis 快照）
  - cancel_job: 用户终止（signal 携带 keep_completed）
  - pause_job / resume_job: 暂停/恢复调度（signal）

状态由 Temporal 管理（Workflow query 返回 Job 快照）；Redis 仅保留
引导快照 + 用户任务索引（list_jobs 分页），以及 BYOK key 的短 TTL 临时桥接。
"""

import asyncio
import time
import uuid

from loguru import logger

from app.agents.orchestration.dag import DagValidationError, execute_dag
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.planner import LlmPlanner, Planner
from app.agents.orchestration.review import ReviewHook, get_reviewer
from app.agents.orchestration.state import RedisStateStore, StateStore
from app.agents.orchestration.workers import WORKERS
from app.core.config import settings


class AgentOrchestrator:
    """多智能体协作编排器（单例，全局复用）.

    temporal_enabled：None 时按 settings.AGENT_ORCHESTRATION 决定；
    测试/显式场景可传 False 强制走自建 DAG（legacy）。
    """

    def __init__(
        self,
        store: StateStore | None = None,
        planner: Planner | None = None,
        workers: dict | None = None,
        review: ReviewHook | None = None,
        temporal_enabled: bool | None = None,
    ):
        self._store = store or RedisStateStore()
        self._planner = planner or LlmPlanner()
        self._workers = workers if workers is not None else WORKERS
        self._review = review or get_reviewer()
        # ── Temporal 模式 ──
        self._temporal_mode = (
            settings.AGENT_ORCHESTRATION == "temporal"
            if temporal_enabled is None
            else bool(temporal_enabled)
        )
        self._temporal_available = False
        self._temporal_probe_at = 0.0
        # ── legacy 自建 DAG 后台任务 ──
        self._tasks: dict[str, asyncio.Task] = {}  # job_id -> 后台执行任务
        # BYOK：legacy 路径的任务内临时 API key（仅内存，任务结束即释放）
        self._job_api_keys: dict[str, str] = {}

    # ── Temporal 可用性探测（成功缓存；失败 30s 后重试）──────────

    async def _probe_temporal(self) -> bool:
        if not self._temporal_mode:
            return False
        if self._temporal_available:
            return True
        if time.monotonic() - self._temporal_probe_at < 30:
            return False
        self._temporal_probe_at = time.monotonic()
        try:
            from app.agents.orchestration.temporal.client import get_temporal_client

            await get_temporal_client()
            self._temporal_available = True
            logger.info("Temporal 已连接: {}", settings.TEMPORAL_ADDRESS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Temporal 不可用（{}），多智能体任务回退自建 DAG", exc)
        return self._temporal_available

    # ── 提交与执行 ───────────────────────────────────────────

    async def submit_job(
        self,
        user_id: str,
        request: str,
        scene: str = "office",
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        llm_api_key: str | None = None,
        clarification_answer: str | None = None,
        office_docs: list[dict] | None = None,
    ) -> Job:
        """规划任务树并启动执行（Temporal 优先），立即返回 Job."""
        # 幂等：30 秒内相同请求且任务未结束 → 直接返回（防双击/重复提交）
        try:
            for jid in await self._store.list_job_ids(user_id, 5):
                j = await self._store.get_job(jid)
                if (
                    j
                    and j.request == request
                    and time.time() - (j.created_at or 0) < 30
                    and j.status
                    in (JobStatus.RUNNING, JobStatus.PENDING, JobStatus.WAITING_APPROVAL)
                ):
                    logger.info("幂等命中：返回已提交的相同任务 {}", jid[:8])
                    return j
        except Exception:  # noqa: BLE001
            pass
        tree = await self._planner.plan(
            user_id,
            request,
            scene,
            project_id,
            project_ids,
            llm_api_key,
            clarification_answer,
            office_docs,
        )
        job = Job(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            request=request,
            scene=scene,
            status=JobStatus.RUNNING,
            nodes=tree.nodes,
            plan_text=tree.plan_text,
        )
        # DAG 静态校验：agent 已注册 / 必选参数 / 无环 / id 唯一；
        # 校验失败 → 降级为知识库检索（简化流程），避免"LLM 瞎指挥"产生必失败的 DAG
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.models import TaskNode

        dag_errors = validate_planned_dag(job.nodes, self._workers)
        if dag_errors:
            logger.warning(
                "任务 DAG 校验失败，降级为检索流程: {} | {}", job.job_id[:8], "；".join(dag_errors)[:300]
            )
            job.nodes = [
                TaskNode(
                    id=f"r{int(time.time())}-{uuid.uuid4().hex[:6]}",
                    name="知识库检索（规划降级）",
                    agent="retrieval",
                    params={"query": request, "top_k": 5},
                    depends_on=[],
                )
            ]
        # 意图不明确：LLM 请求向用户澄清，任务以"待澄清"结果直接收敛
        if tree.clarification and not tree.nodes:
            job.status = JobStatus.COMPLETED
            job.result = {"type": "clarification", "question": tree.clarification}

        if await self._probe_temporal():
            try:
                await self._submit_temporal(job, llm_api_key)
                logger.info(
                    "多智能体任务已提交(Temporal): {} | agent={} request={}",
                    job.job_id[:8],
                    [n.agent for n in job.nodes],
                    request[:40],
                )
                return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 提交任务失败，回退自建 DAG: {} | {}", job.job_id[:8], exc)

        # ── legacy 自建 DAG 回退 ──
        await self._store.create_job(job)
        if llm_api_key:
            self._job_api_keys[job.job_id] = llm_api_key
        task = asyncio.create_task(self._run_job(job.job_id))
        self._tasks[job.job_id] = task
        logger.info(
            "多智能体任务已提交(legacy): {} | agent={} request={}",
            job.job_id[:8],
            [n.agent for n in job.nodes],
            request[:40],
        )
        return job

    async def _submit_temporal(self, job: Job, llm_api_key: str | None) -> None:
        """启动 Temporal Workflow：payload = Job dict + 节点执行配置."""
        from app.agents.orchestration.temporal.client import (
            start_agent_workflow,
            store_byok_key,
        )

        payload = job.model_dump()
        payload["config"] = {
            "node_timeout_seconds": settings.AGENT_NODE_TIMEOUT_SECONDS,
            "node_max_retries": settings.AGENT_NODE_MAX_RETRIES,
            "node_concurrency": settings.AGENT_NODE_CONCURRENCY,
        }
        if llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_agent_workflow(payload, job.job_id)
        # 引导快照 + 用户任务索引（list_jobs 分页；workflow 运行后以 Temporal 状态为准）
        await self._store.create_job(job)

    async def _run_job(self, job_id: str) -> None:
        """legacy 后台执行：校验 DAG → 拓扑执行 → 汇总状态."""
        llm_api_key = self._job_api_keys.pop(job_id, None)
        try:
            job = await self._store.get_job(job_id)
            if job is None:
                return
            await execute_dag(
                job,
                self._workers,
                self._review,
                self._store,
                llm_api_key=llm_api_key,
            )
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
            self._job_api_keys.pop(job_id, None)

    # ── 查询 ────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> Job | None:
        """优先查询 Temporal 工作流状态，失败回退 Redis 快照."""
        job = None
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import query_agent_job

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 Temporal 任务状态失败，回退快照 {}: {}", job_id, exc)
        if job is None:
            job = await self._store.get_job(job_id)
        return await self._attach_progress(job) if job else None

    async def _attach_progress(self, job: Job) -> Job:
        """把 Redis 中的节点实时进度合并进 node.metadata["progress"]（仅展示用）."""
        try:
            from app.agents.core.progress import get_job_progress

            progress = await get_job_progress(job.job_id)
            if progress:
                for n in job.nodes:
                    text = progress.get(n.id)
                    if text:
                        n.metadata = {**(n.metadata or {}), "progress": str(text)}
        except Exception as exc:  # noqa: BLE001
            logger.debug("合并任务进度失败 {}: {}", job.job_id, exc)
        return job

    async def list_jobs(self, user_id: str, limit: int = 20) -> list[Job]:
        """按用户索引倒序列出任务（Temporal/legacy 混合列表均支持）."""
        ids = await self._store.list_job_ids(user_id, limit)
        jobs: list[Job] = []
        for jid in ids:
            job = await self.get_job(jid)
            if job:
                jobs.append(job)
        return jobs

    # ── 控制：终止 / 暂停 / 恢复 ─────────────────────────────

    async def cancel_job(self, job_id: str, keep_completed: bool = True) -> Job | None:
        """终止任务：Temporal 路径发 cancel_request signal（可保留已完成节点）."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    cancel_agent_workflow,
                    query_agent_job,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status in (
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                    ):
                        return job
                    await cancel_agent_workflow(job_id, keep_completed)
                    job.status = JobStatus.CANCELLED
                    job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 取消任务失败，回退快照 {}: {}", job_id, exc)

        # legacy：状态驱动，execute_dag 轮询到 CANCELLED 后自行收尾
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

    async def approve_job(self, job_id: str, node_id: str, approved: bool = True) -> None:
        """人工审批：向工作流发送 approve_task 信号（高风险节点门控）."""
        if not await self._probe_temporal():
            raise RuntimeError("Temporal 不可用，无法审批")
        from app.agents.orchestration.temporal.client import approve_agent_workflow

        await approve_agent_workflow(job_id, node_id, approved)

    async def pause_job(self, job_id: str) -> Job | None:
        """暂停任务（不调度新节点；运行中的节点会执行完）."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    pause_agent_workflow,
                    query_agent_job,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status == JobStatus.RUNNING:
                        await pause_agent_workflow(job_id)
                        job.status = JobStatus.PAUSED
                        job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 暂停任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return job
        job.status = JobStatus.PAUSED
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job

    async def resume_job(self, job_id: str) -> Job | None:
        """恢复被暂停的任务."""
        if await self._probe_temporal():
            try:
                from app.agents.orchestration.temporal.client import (
                    query_agent_job,
                    resume_agent_workflow,
                )

                snap = await query_agent_job(job_id)
                if snap is not None:
                    job = Job.model_validate(snap)
                    if job.status == JobStatus.PAUSED:
                        await resume_agent_workflow(job_id)
                        job.status = JobStatus.RUNNING
                        job.updated_at = time.time()
                    return job
            except Exception as exc:  # noqa: BLE001
                logger.warning("Temporal 恢复任务失败，回退快照 {}: {}", job_id, exc)

        job = await self._store.get_job(job_id)
        if job is None or job.status != JobStatus.PAUSED:
            return job
        job.status = JobStatus.RUNNING
        job.updated_at = time.time()
        await self._store.save_job(job)
        return job


# 全局单例（API 层使用）
orchestrator = AgentOrchestrator()
