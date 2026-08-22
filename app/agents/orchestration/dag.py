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
    "direct_llm": ["instruction"],
    "collect_results": ["items"],
    "atomic_step": ["instruction", "preferred_tool"],
    "react_step": ["instruction"],
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
    llm_config: dict | None = None,
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
        node.metadata.setdefault("tool_index", 0)
        from app.agents.orchestration.context import build_dependency_context_from_refs

        node.metadata["dependency_results"] = await build_dependency_context_from_refs(
            node, node_by_id, user_id=job.user_id
        )

        from app.agents.orchestration.effects import begin_effect, finish_effect
        from app.agents.orchestration.safety import is_effectful

        effectful = is_effectful(node)
        # A confirmation denial happens before the tool body starts.  It must
        # never be journaled as an uncertain side effect, otherwise the later
        # user approval cannot safely resume the exact same node.
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

        from app.agents.orchestration.langgraph_runner import LangGraphNodeRunner

        async def on_running(_attempt: int) -> None:
            node.status = TaskStatus.RUNNING
            node.started_at = node.started_at or time.time()
            node.error = None
            node.error_code = None
            await store.save_job(job)
            try:
                from app.agents.orchestration.execution_lineage import record_node_span

                await record_node_span(
                    execution_id=job.execution_id or job.job_id,
                    job_id=job.job_id,
                    node=node,
                    event="started",
                )
            except Exception:  # noqa: BLE001
                pass

        async def on_retry(attempt: int) -> None:
            node.retries = attempt
            node.status = TaskStatus.RETRYING
            await store.save_job(job)

        try:
            outcome = await LangGraphNodeRunner(
                worker=worker,
                node=node,
                ctx=_ctx(node),
                review=review,
                timeout_seconds=settings.AGENT_NODE_TIMEOUT_SECONDS,
                max_retries=0 if effectful else node.max_retries,
                effectful=effectful,
                on_running=on_running,
                on_retry=on_retry,
            ).run()
        except asyncio.CancelledError:
            node.status = TaskStatus.INTERRUPTED
            node.completed_at = time.time()
            node.error = "任务被中断"
            if effectful and node.idempotency_key:
                node.effect_status = "uncertain"
                await finish_effect(node.idempotency_key, "uncertain")
            await store.save_job(job)
            raise

        node.retries = outcome.retries
        if outcome.success:
            from app.agents.orchestration.presentation import attach_display_result

            node.status = TaskStatus.COMPLETED
            node.result = attach_display_result(node, outcome.result or {})
            if effectful and node.idempotency_key:
                node.effect_status = "committed"
                await finish_effect(node.idempotency_key, "committed", outcome.result)
        else:
            node.status = TaskStatus.ESCALATED if outcome.escalation else TaskStatus.FAILED
            # L2 arbitration may need the attempted tool/compact evidence to
            # construct an approval gate. Preserve only the worker's normal
            # public result object; server internals remain redacted by the
            # worker/MCP boundary.
            node.result = outcome.result or None
            node.error = outcome.error or "执行失败"
            node.error_code = outcome.error_code or "EXEC_ERROR"
            if outcome.recovery:
                node.metadata["recovery"] = outcome.recovery
            if outcome.escalation:
                node.metadata["escalation"] = outcome.escalation
            if effectful and node.idempotency_key and not (
                outcome.escalation
                and str((outcome.escalation or {}).get("reason") or "") == "approval_required"
            ):
                node.effect_status = "uncertain"
                await finish_effect(node.idempotency_key, "uncertain")
            elif effectful and node.idempotency_key:
                from app.agents.orchestration.effects import abandon_pending_effect

                # The reservation was created before the worker discovered a
                # confirmation requirement; no side effect has run yet.
                await abandon_pending_effect(node.idempotency_key)
                node.effect_status = "pending"
        node.completed_at = time.time()
        try:
            from app.agents.orchestration.execution_lineage import (
                ensure_node_result_ref,
                record_node_span,
            )

            if node.status == TaskStatus.COMPLETED:
                await ensure_node_result_ref(job.user_id, node)
            await record_node_span(
                execution_id=job.execution_id or job.job_id,
                job_id=job.job_id,
                node=node,
                event="finished",
            )
        except Exception:  # noqa: BLE001
            pass
        await store.save_job(job)
        return

    def _ctx(node: TaskNode):
        from app.agents.orchestration.workers import WorkerContext
        from app.services.office_stream import push_delta

        async def on_output(text: str) -> None:
            await push_delta(job.job_id, node.id, text)

        return WorkerContext(
            user_id=job.user_id,
            job_id=job.job_id,
            scene=job.scene,
            user_role=job.user_role,
            llm_api_key=llm_api_key,
            llm_config=llm_config,
            user_request=job.request,
            confirmed_tools=frozenset(
                str(value) for value in ((node.metadata or {}).get("confirmed_tools") or [])
            ),
            confirmed_tool_calls=frozenset(
                str(value) for value in ((node.metadata or {}).get("confirmed_tool_calls") or [])
            ),
            on_output=on_output,
        )

    # ── 依赖驱动的事件调度循环 ──
    # 任一节点结束即重新计算可运行节点，不再等待同一 ready 批次全部结束。
    terminal_statuses = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.SKIPPED,
        TaskStatus.CANCELLED,
        TaskStatus.INTERRUPTED,
        TaskStatus.ESCALATED,
    }
    # A resumed DAG may already contain terminal nodes.  Keep successful
    # dependencies distinct from merely settled dependencies: normal DAG nodes
    # still require success, while explicit task-manifest entries may continue
    # after an independently failed prior checklist item.
    pending = {n.id for n in job.nodes if n.status not in terminal_statuses}
    completed = {n.id for n in job.nodes if n.status == TaskStatus.COMPLETED}
    settled = {n.id for n in job.nodes if n.status in terminal_statuses}
    running_by_id: dict[str, asyncio.Task] = {}

    async def _with_sem(node: TaskNode) -> None:
        async with sem:
            from app.agents.orchestration.channel_limits import channel_limiter

            channel = str((node.metadata or {}).get("route_channel") or "agent")
            async with channel_limiter.claim(
                channel, lease_seconds=max(60, int(settings.AGENT_NODE_TIMEOUT_SECONDS) + 60)
            ):
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
            if not node.metadata.get("continue_on_dependency_failure") and any(
                node_by_id[d].status
                in (
                    TaskStatus.FAILED,
                    TaskStatus.SKIPPED,
                    TaskStatus.CANCELLED,
                    TaskStatus.INTERRUPTED,
                    TaskStatus.ESCALATED,
                )
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
        # 清单节点显式声明失败可继续，因此以终态而非成功态解除其串行依赖。
        for nid in list(pending):
            node = node_by_id[nid]
            dependency_state = (
                settled
                if node.metadata.get("continue_on_dependency_failure")
                else completed
            )
            if all(d in dependency_state for d in node.depends_on):
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
            if node_by_id[nid].status in terminal_statuses:
                settled.add(nid)

    # ── 汇总任务状态 ──
    job = await store.get_job(job.job_id) or job
    statuses = [n.status for n in job.nodes]
    if job.status not in (JobStatus.CANCELLED, JobStatus.INTERRUPTED, JobStatus.PAUSED):
        if all(s == TaskStatus.COMPLETED for s in statuses):
            job.status = JobStatus.COMPLETED
        elif any(s in (TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.ESCALATED) for s in statuses):
            job.status = JobStatus.FAILED
            if not job.error:
                failed = next(
                    (node for node in job.nodes if node.status in (TaskStatus.FAILED, TaskStatus.ESCALATED) and node.error),
                    None,
                )
                if failed:
                    job.error = failed.error
        else:
            job.status = JobStatus.RUNNING
    job.updated_at = time.time()
    await store.save_job(job)
    return job
