"""DAG 任务编排执行器 —— 轻量自建（asyncio + 状态存储），不上 Temporal.

特性：
  - 拓扑排序执行：节点依赖全部完成后才就绪
  - 并发上限（资源协调）：同时最多执行 AGENT_NODE_CONCURRENCY 个节点
  - React 重试：节点失败按 retryable 重试，最多 max_retries  次
  - 质检钩子：节点产出结果后走 ReviewHook，不通过则重试/失败
  - 暂停/取消：执行循环感知任务状态，取消时立即中断运行中的节点
"""

import asyncio
import time

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.state import StateStore
from app.core.config import settings


class DagValidationError(Exception):
    """DAG 结构非法（环 / 依赖缺失 / id 重复）."""


def validate_dag(nodes: list[TaskNode]) -> None:
    """校验 DAG：id 唯一、依赖存在、无环（Kahn 拓扑检测）."""
    ids = {n.id for n in nodes}
    if len(ids) != len(nodes):
        raise DagValidationError("任务节点 id 重复")
    for n in nodes:
        missing = [d for d in n.depends_on if d not in ids]
        if missing:
            raise DagValidationError(f"节点 {n.id} 依赖不存在: {missing}")
    # Kahn 环检测
    indegree = {n.id: 0 for n in nodes}
    children: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for d in n.depends_on:
            children[d].append(n.id)
            indegree[n.id] += 1
    queue = [nid for nid, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        nid = queue.pop()
        visited += 1
        for c in children[nid]:
            indegree[c] -= 1
            if indegree[c] == 0:
                queue.append(c)
    if visited != len(nodes):
        raise DagValidationError("任务依赖存在环，无法执行")


async def execute_dag(
    job: Job,
    workers: dict,
    review,
    store: StateStore,
    *,
    concurrency: int | None = None,
    llm_api_key: str | None = None,
) -> Job:
    """执行整个 DAG；就地更新 job.nodes 状态并持久化."""
    validate_dag(job.nodes)
    concurrency = concurrency or settings.AGENT_NODE_CONCURRENCY
    sem = asyncio.Semaphore(max(1, concurrency))
    # 以 store 中的任务为权威对象（cancel/pause 由 API 写入 store）；
    # 全程只操作这一个 job 对象，避免"副本节点对象"导致状态写不回去。
    stored = await store.get_job(job.job_id)
    if stored is not None:
        job = stored
    node_by_id = {n.id: n for n in job.nodes}

    async def sync_status() -> None:
        """从 store 同步任务级状态（cancel/pause 由外部写入），不替换节点对象."""
        snap = await store.get_job(job.job_id)
        if snap is not None and snap.status != job.status:
            job.status = snap.status

    async def run_node(node: TaskNode) -> None:
        """单个节点：执行 + 质检 + 重试（React 多轮）."""
        worker = workers.get(node.agent)
        if worker is None:
            node.status = TaskStatus.FAILED
            node.error = f"未注册的执行 agent: {node.agent}"
            node.error_code = "AGENT_NOT_FOUND"
            node.completed_at = time.time()
            await store.save_job(job)
            return

        for attempt in range(node.max_retries + 1):
            if attempt:
                node.retries = attempt
                node.status = TaskStatus.RETRYING
                await store.save_job(job)
            node.status = TaskStatus.RUNNING
            node.started_at = time.time()
            node.error = None
            await store.save_job(job)
            try:
                result = await asyncio.wait_for(
                    worker.execute(node, _ctx()),
                    timeout=settings.AGENT_NODE_TIMEOUT_SECONDS,
                )
                # worker 显式返回失败（如技能未命中/参数错误）→ 按节点失败处理
                if isinstance(result, dict) and result.get("success") is False:
                    node.error = str(result.get("error") or "执行失败")
                    node.error_code = str(result.get("error_code") or "EXEC_ERROR")
            except asyncio.CancelledError:
                node.status = TaskStatus.INTERRUPTED
                node.completed_at = time.time()
                node.error = "任务被中断"
                await store.save_job(job)
                raise
            except asyncio.TimeoutError:
                node.error = f"执行超时（>{settings.AGENT_NODE_TIMEOUT_SECONDS}s）"
                node.error_code = "TIMEOUT"
            except Exception as exc:  # noqa: BLE001
                logger.warning("节点 {} 执行异常: {}", node.id, exc)
                node.error = str(exc)
                node.error_code = "EXEC_ERROR"

            if node.error:
                # React 重试：瞬时/超时类错误可重试
                if attempt < node.max_retries:
                    continue
                node.status = TaskStatus.FAILED
                node.completed_at = time.time()
                await store.save_job(job)
                return

            # 质检
            verdict = await review.review(node, result, _ctx())
            if verdict.approved:
                node.status = TaskStatus.COMPLETED
                node.result = result
                node.completed_at = time.time()
                await store.save_job(job)
                return
            node.error = f"质检未通过: {verdict.feedback}"
            node.error_code = "REVIEW_REJECTED"
            if attempt < node.max_retries:
                continue
            node.status = TaskStatus.FAILED
            node.completed_at = time.time()
            await store.save_job(job)
            return

    def _ctx():
        from app.agents.orchestration.workers import WorkerContext

        return WorkerContext(
            user_id=job.user_id,
            job_id=job.job_id,
            scene=job.scene,
            llm_api_key=llm_api_key,
        )

    # ── 依赖驱动的调度循环 ──
    pending = {n.id for n in job.nodes}
    completed: set[str] = set()
    running_tasks: set[asyncio.Task] = set()

    while pending:
        await sync_status()
        # 暂停：不调度新节点，等待恢复
        if job.status == JobStatus.PAUSED:
            await asyncio.sleep(1)
            continue
        # 取消/中断：立即终止运行中的节点
        if job.status in (JobStatus.CANCELLED, JobStatus.INTERRUPTED):
            for t in running_tasks:
                t.cancel()
            for nid in pending:
                node = node_by_id[nid]
                if node.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING):
                    node.status = (
                        TaskStatus.CANCELLED if job.status == JobStatus.CANCELLED
                        else TaskStatus.INTERRUPTED
                    )
                    node.error = "任务被用户终止"
            await store.save_job(job)
            return

        ready = [
            node_by_id[nid]
            for nid in pending
            if all(d in completed for d in node_by_id[nid].depends_on)
        ]
        if not ready:
            # 没有可执行节点 → 有节点失败导致依赖链断裂，其余标记跳过
            for nid in pending:
                node_by_id[nid].status = TaskStatus.SKIPPED
                node_by_id[nid].error = "前置依赖失败"
            await store.save_job(job)
            return

        async def _with_sem(node: TaskNode) -> None:
            async with sem:
                await run_node(node)

        running_tasks = {asyncio.create_task(_with_sem(n)) for n in ready}
        # 等待期间每秒轮询任务状态：取消/中断时立即终止运行中的节点
        while running_tasks:
            await sync_status()
            if job.status in (JobStatus.CANCELLED, JobStatus.INTERRUPTED):
                for t in running_tasks:
                    t.cancel()
                await asyncio.gather(*running_tasks, return_exceptions=True)
                for nid in pending:
                    node = node_by_id[nid]
                    # 运行中的节点已由 run_node 的 CancelledError 处理标记为 INTERRUPTED
                    if node.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RETRYING):
                        node.status = (
                            TaskStatus.CANCELLED if job.status == JobStatus.CANCELLED
                            else TaskStatus.INTERRUPTED
                        )
                        node.error = "任务被用户终止"
                await store.save_job(job)
                return
            done, running_tasks = await asyncio.wait(running_tasks, timeout=1.0)
            for t in done:
                try:
                    t.result()  # 收集异常/CancelledError
                except asyncio.CancelledError:
                    pass

        for n in ready:
            pending.discard(n.id)
            if n.status == TaskStatus.COMPLETED:
                completed.add(n.id)

    # ── 汇总任务状态 ──
    job = await store.get_job(job.job_id) or job
    statuses = [n.status for n in job.nodes]
    if job.status not in (JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED):
        if all(s == TaskStatus.COMPLETED for s in statuses):
            job.status = JobStatus.COMPLETED
        elif any(s in (TaskStatus.FAILED, TaskStatus.SKIPPED) for s in statuses):
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.RUNNING
    job.updated_at = time.time()
    await store.save_job(job)
    return job
