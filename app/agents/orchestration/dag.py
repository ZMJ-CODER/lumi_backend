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


# 各 agent 的必选参数（校验规划结果用）
_REQUIRED_PARAMS: dict[str, list[str]] = {
    "atomic_step": ["instruction", "preferred_tool"],
    "office_doc": ["doc_id", "instruction", "mode"],
    "office_text": ["instruction"],
    "office_research": ["instruction", "mode"],
    "office_todo": ["action"],
    "retrieval": ["query"],
    "web_research": ["instruction"],
    "code": ["project_id", "instruction"],
    "code_reader": ["project_id", "instruction"],
    "code_writer": ["project_id", "instruction"],
}


def validate_planned_dag(
    nodes: list[TaskNode],
    workers: dict | None = None,
) -> list[str]:
    """规划结果静态校验：agent 已注册、必选参数、无环、id 唯一.

    Returns 错误列表（空 = 通过）。
    """
    if workers is None:
        from app.agents.orchestration.workers import WORKERS

        workers = WORKERS

    errors: list[str] = []
    seen: set[str] = set()
    for n in nodes:
        if n.id in seen:
            errors.append(f"节点 id 重复: {n.id}")
        seen.add(n.id)
        if n.agent not in workers:
            errors.append(f"agent 未注册: {n.agent}")
            continue
        for p in _REQUIRED_PARAMS.get(n.agent, []):
            if not n.params.get(p):
                errors.append(f"{n.agent} 缺少必选参数 {p}")
    try:
        validate_dag(nodes)
    except DagValidationError as exc:
        errors.append(str(exc))
    return errors


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
    from app.agents.orchestration.resources import resource_coordinator
    from app.agents.orchestration.safety import prepare_node_safety

    # 以 store 中的任务为权威对象（cancel/pause 由 API 写入 store）；
    # 全程只操作这一个 job 对象，避免"副本节点对象"导致状态写不回去。
    stored = await store.get_job(job.job_id)
    if stored is not None:
        job = stored
    else:
        await store.create_job(job)
    node_by_id = {n.id: n for n in job.nodes}
    for node in job.nodes:
        prepare_node_safety(node, job.user_id, job.job_id)

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

        # 只把显式依赖节点的结果注入本步骤。无依赖的并行步骤没有共享可变上下文，
        # 除各自外部工具副作用外不会读写彼此的执行结果。
        node.metadata = dict(node.metadata or {})
        from app.agents.orchestration.context import build_dependency_context

        node.metadata["dependency_results"] = build_dependency_context(node, node_by_id)

        from app.agents.orchestration.effects import begin_effect, finish_effect
        from app.agents.orchestration.safety import is_effectful

        effectful = is_effectful(node)
        if effectful and node.idempotency_key:
            created, existing = await begin_effect(node.idempotency_key)
            if not created:
                status = str((existing or {}).get("status") or "uncertain")
                if status == "committed" and isinstance((existing or {}).get("result"), dict):
                    node.status = TaskStatus.COMPLETED
                    node.result = (existing or {})["result"]
                    node.effect_status = "committed"
                    node.completed_at = time.time()
                    await store.save_job(job)
                    return
                node.status = TaskStatus.FAILED
                node.error_code = "EFFECT_UNCERTAIN"
                node.error = "副作用步骤已开始但结果不确定，已停止自动重试以避免重复执行"
                node.effect_status = "uncertain"
                node.completed_at = time.time()
                await store.save_job(job)
                return

        attempts = 1 if effectful else node.max_retries + 1
        for attempt in range(attempts):
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
                    logger.warning(
                        "节点 {} 执行失败: {} | {} | {}",
                        node.id,
                        node.name,
                        node.error_code,
                        str(node.error)[:300],
                    )
            except asyncio.CancelledError:
                node.status = TaskStatus.INTERRUPTED
                node.completed_at = time.time()
                node.error = "任务被中断"
                if effectful and node.idempotency_key:
                    node.effect_status = "uncertain"
                    await finish_effect(node.idempotency_key, "uncertain")
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
                if effectful and node.idempotency_key:
                    node.effect_status = "uncertain"
                    await finish_effect(node.idempotency_key, "uncertain")
                # React 重试：瞬时/超时类错误可重试
                if attempt + 1 < attempts:
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
                if effectful and node.idempotency_key:
                    node.effect_status = "committed"
                    await finish_effect(node.idempotency_key, "committed", result)
                node.completed_at = time.time()
                await store.save_job(job)
                return
            node.error = f"质检未通过: {verdict.feedback}"
            node.error_code = "REVIEW_REJECTED"
            if attempt + 1 < attempts:
                continue
            if effectful and node.idempotency_key:
                node.effect_status = "uncertain"
                await finish_effect(node.idempotency_key, "uncertain")
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
            user_role=job.user_role,
            llm_api_key=llm_api_key,
        )

    # ── 依赖驱动的事件调度循环 ──
    # 任一节点结束即重新计算可运行节点，不再等待同一 ready 批次全部结束。
    pending = {n.id for n in job.nodes}
    completed: set[str] = set()
    running_by_id: dict[str, asyncio.Task] = {}

    async def _with_sem(node: TaskNode) -> None:
        async with sem:
            async with resource_coordinator.claim(
                node.resource_claims,
                ttl=max(60, int(settings.AGENT_NODE_TIMEOUT_SECONDS) + 60),
            ):
                await run_node(node)

    while pending or running_by_id:
        await sync_status()
        # 暂停：不调度新节点，等待恢复
        if job.status == JobStatus.PAUSED:
            await asyncio.sleep(1)
            continue
        # 取消/中断：立即终止运行中的节点
        if job.status in (JobStatus.CANCELLED, JobStatus.INTERRUPTED):
            running_ids = list(running_by_id)
            running_tasks = list(running_by_id.values())
            for t in running_tasks:
                t.cancel()
            if running_tasks:
                # 等待节点的 CancelledError 收尾完成，确保副作用日志和节点终态
                # 已落盘后再退出 DAG，避免后台 task 随事件循环结束而丢失更新。
                await asyncio.gather(*running_tasks, return_exceptions=True)
            for nid in running_ids:
                node = node_by_id[nid]
                if node.status in (
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.RETRYING,
                ):
                    node.status = TaskStatus.INTERRUPTED
                    node.completed_at = time.time()
                    node.error = "任务被用户终止"
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

        # 依赖明确失败的节点无需等待其他分支，立即原子化跳过。
        changed = False
        for nid in list(pending):
            node = node_by_id[nid]
            if any(
                node_by_id[d].status
                in (TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED, TaskStatus.INTERRUPTED)
                for d in node.depends_on
            ):
                node.status = TaskStatus.SKIPPED
                node.error = "前置依赖失败"
                node.completed_at = time.time()
                pending.discard(nid)
                changed = True
        if changed:
            await store.save_job(job)

        # 调度所有依赖已完成的步骤；它们只受 semaphore 限流，不形成批次屏障。
        for nid in list(pending):
            node = node_by_id[nid]
            if all(d in completed for d in node.depends_on):
                node.status = TaskStatus.READY
                await store.save_job(job)
                running_by_id[nid] = asyncio.create_task(_with_sem(node))
                pending.discard(nid)

        if not running_by_id:
            if pending:
                for nid in pending:
                    node_by_id[nid].status = TaskStatus.SKIPPED
                    node_by_id[nid].error = "前置依赖未完成"
                await store.save_job(job)
            break

        done, _ = await asyncio.wait(
            set(running_by_id.values()),
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for nid, task in list(running_by_id.items()):
            if task not in done:
                continue
            running_by_id.pop(nid, None)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            if node_by_id[nid].status == TaskStatus.COMPLETED:
                completed.add(nid)

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
