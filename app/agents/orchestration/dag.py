"""DAG 任务编排执行器 —— 轻量自建（asyncio + 状态存储），不上 Temporal.

特性：
  - 拓扑排序执行：节点依赖全部完成后才就绪
  - 并发上限（资源协调）：同时最多执行 AGENT_NODE_CONCURRENCY 个节点
  - React 重试：节点失败按 retryable 重试，最多 max_retries  次
  - 质检钩子：节点产出结果后走 ReviewHook，不通过则重试/失败
  - 暂停/取消：执行循环感知任务状态，取消时立即中断运行中的节点
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping

from lumi_orch.dag import DagValidationError, decide_next_nodes, validate_dag
from lumi_orch.ports import JobStateStorePort, NodeWorkerPort, ReviewPort

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.core.config import settings


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
    "document_targeting": ["query", "office_docs"],
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
    workers: Mapping[str, NodeWorkerPort],
    review: ReviewPort,
    store: JobStateStorePort,
    *,
    concurrency: int | None = None,
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    on_waiting_resources: Callable[[Job], Awaitable[None]] | None = None,
    ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
) -> Job:
    """执行整个 DAG；就地更新 job.nodes 状态并持久化."""
    validate_dag(job.nodes)
    concurrency = concurrency or settings.AGENT_NODE_CONCURRENCY
    sem = asyncio.Semaphore(max(1, concurrency))
    from app.agents.orchestration.resources import (
        WriteResourceCoordinationUnavailable,
        resource_coordinator,
    )
    from app.agents.orchestration.safety import prepare_node_safety
    from app.agents.orchestration.node_timeouts import node_timeout_seconds

    def _node_timeout(node: TaskNode) -> int:
        return node_timeout_seconds(node)

    def _approval_probe(node: TaskNode) -> bool:
        """Let a confirmation-gated worker emit its approval request first."""
        tool = str((node.params or {}).get("preferred_tool") or "")
        if not tool:
            return False
        try:
            from app.agents.skills.registry import SkillRegistry

            skill = SkillRegistry.get(tool)
            if skill is None:
                # Third-party/test workers own their confirmation protocol;
                # there is no registered platform resource contract to lock.
                return True
            if (node.metadata or {}).get("confirmed_tool_calls"):
                return False
            return bool(skill.requires_confirmation)
        except Exception:  # noqa: BLE001
            return True
        # Compatibility for test/third-party workers whose confirmation policy
        # is returned only after the probe call (the executor still enforces
        # the approval fingerprint before any side effect).
        lower = tool.casefold()
        return any(token in lower for token in ("delete", "remove", "send", "write", "edit", "pay", "kill"))

    async def _enter_waiting_resources(node: TaskNode) -> None:
        """Persist a bounded wait and release active admission capacity once."""
        node.status = TaskStatus.PENDING
        node.metadata = dict(node.metadata or {})
        node.metadata["waiting_resources"] = True
        node.error = "写资源协调服务暂不可用，任务将自动等待恢复"
        node.error_code = "RESOURCE_COORDINATION_UNAVAILABLE"
        job.status = JobStatus.WAITING_RESOURCES
        job.routing = dict(job.routing or {})
        job.routing.setdefault("waiting_resources_started_at", time.time())
        if not job.routing.get("admission_released_while_waiting"):
            # Mark before the callback: a process crash after release must not
            # make a resumed job look as if it still owns an active slot.
            job.routing["admission_released_while_waiting"] = True
            await store.save_job(job)
            if on_waiting_resources is not None:
                await on_waiting_resources(job)
        await store.save_job(job)

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
        dependency_hashes: list[str] = []
        for dependency_id in node.depends_on:
            dependency = node_by_id.get(dependency_id)
            if dependency is None:
                continue
            result_ref = (dependency.metadata or {}).get("result_ref")
            digest = str((result_ref or {}).get("sha256") or "") if isinstance(result_ref, dict) else ""
            if not digest and dependency.result:
                raw = json.dumps(dependency.result, ensure_ascii=False, sort_keys=True, default=str)
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if digest:
                dependency_hashes.append(digest)
        node.metadata["approval_upstream_sha256"] = (
            hashlib.sha256("|".join(sorted(dependency_hashes)).encode("utf-8")).hexdigest()
            if dependency_hashes else ""
        )

        from app.agents.orchestration.effects import (
            EffectJournalUnavailable,
            confirm_effect,
            effect_intent_for_node,
            mark_effect_uncertain,
            record_effect_intent,
        )
        from app.agents.orchestration.safety import is_effectful

        effectful = is_effectful(node)
        # A confirmation denial happens before the tool body starts.  It must
        # never be journaled as an uncertain side effect, otherwise the later
        # user approval cannot safely resume the exact same node.
        if effectful and node.idempotency_key:
            try:
                created, existing = await record_effect_intent(
                    node.idempotency_key,
                    effect_intent_for_node(job_id=job.job_id, node=node),
                )
            except EffectJournalUnavailable:
                # A write must never start without a durable intent record.
                node.status = TaskStatus.FAILED
                node.error_code = "EFFECT_JOURNAL_UNAVAILABLE"
                node.error = "副作用安全日志不可用，已阻止执行以避免重复操作"
                node.effect_status = "pending"
                node.completed_at = time.time()
                await store.save_job(job)
                return
            if not created:
                status = str((existing or {}).get("status") or "uncertain")
                if status in {"committed", "confirmed"} and isinstance((existing or {}).get("result"), dict):
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
                timeout_seconds=_node_timeout(node),
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
                try:
                    await mark_effect_uncertain(node.idempotency_key, "task_cancelled")
                except EffectJournalUnavailable:
                    # The durable intent remains; do not turn cancellation into
                    # a retryable write on the next resume.
                    node.error_code = "EFFECT_JOURNAL_UNAVAILABLE"
            await store.save_job(job)
            raise

        node.retries = outcome.retries
        if outcome.success:
            from app.agents.orchestration.presentation import attach_display_result

            node.status = TaskStatus.COMPLETED
            node.result = attach_display_result(node, outcome.result or {})
            if effectful and node.idempotency_key:
                try:
                    await confirm_effect(node.idempotency_key, outcome.result)
                    node.effect_status = "committed"
                except EffectJournalUnavailable:
                    # The tool body may already have succeeded. Its pre-write
                    # intent remains authoritative, so fail closed as uncertain.
                    node.status = TaskStatus.FAILED
                    node.error_code = "EFFECT_JOURNAL_UNAVAILABLE"
                    node.error = "副作用已执行但安全日志确认失败，已停止自动重试"
                    node.effect_status = "uncertain"
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
            if isinstance(outcome.result, dict) and isinstance(outcome.result.get("tool_metadata"), dict):
                node.metadata["tool_metadata"] = dict(outcome.result["tool_metadata"])
            if effectful and node.idempotency_key and not (
                outcome.escalation
                and str((outcome.escalation or {}).get("reason") or "") == "approval_required"
            ):
                node.effect_status = "uncertain"
                try:
                    await mark_effect_uncertain(node.idempotency_key, node.error_code or "execution_failed")
                except EffectJournalUnavailable:
                    node.error_code = "EFFECT_JOURNAL_UNAVAILABLE"
                    node.error = "副作用执行状态无法写入安全日志，已停止自动重试"
            elif effectful and node.idempotency_key:
                from app.agents.orchestration.effects import abandon_pending_effect

                # The reservation was created before the worker discovered a
                # confirmation requirement; no side effect has run yet.
                try:
                    await abandon_pending_effect(node.idempotency_key)
                    node.effect_status = "pending"
                except EffectJournalUnavailable:
                    node.status = TaskStatus.FAILED
                    node.error_code = "EFFECT_JOURNAL_UNAVAILABLE"
                    node.error = "审批前副作用日志无法清理，已停止自动重试"
                    node.effect_status = "uncertain"
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
            approval_context_sha256=str((node.metadata or {}).get("approval_upstream_sha256") or ""),
            office_doc_ids=tuple(
                str(item.get("doc_id"))
                for item in (node.params.get("office_docs") or [])
                if isinstance(item, dict) and item.get("doc_id")
            ) or tuple(str(value) for value in (node.params.get("doc_ids") or []) if str(value)),
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
        # A write is never allowed to fall back to an in-process lock when the
        # distributed coordinator is unavailable. Do this before taking the
        # scarce channel lease (the agent channel has only a few slots).
        probe = _approval_probe(node)
        claims = [] if probe else node.resource_claims
        if not await resource_coordinator.write_coordination_available(claims):
            await _enter_waiting_resources(node)
            return
        async with sem:
            from app.agents.orchestration.channel_limits import channel_limiter

            channel = str((node.metadata or {}).get("route_channel") or "agent")
            timeout = _node_timeout(node)
            try:
                async with channel_limiter.claim(
                    channel, lease_seconds=max(60, timeout + 60)
                ):
                    async with resource_coordinator.claim(
                        claims,
                        ttl=max(60, timeout + 60),
                    ):
                        await run_node(node)
            except WriteResourceCoordinationUnavailable:
                # Redis may fail after the preflight check. No tool body (and
                # therefore no effect intent) has run at this point.
                await _enter_waiting_resources(node)

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

        # A failed distributed write-lock service pauses only the affected
        # write nodes. Independent/read-only nodes retain their fail-open
        # behavior and may continue to completion.
        waiting_resources = {
            nid for nid in pending
            if bool((node_by_id[nid].metadata or {}).get("waiting_resources"))
        }
        if job.status == JobStatus.WAITING_RESOURCES and waiting_resources:
            started_at = float((job.routing or {}).get("waiting_resources_started_at") or time.time())
            wait_limit = max(60, int(settings.AGENT_WAITING_RESOURCES_TIMEOUT_SECONDS))
            if time.time() - started_at >= wait_limit:
                job.status = JobStatus.PAUSED
                job.error = "写资源协调服务在等待时限内未恢复，任务已暂停，请恢复服务后手动继续。"
                job.routing = dict(job.routing or {})
                job.routing["resource_wait_timeout"] = True
                await store.save_job(job)
                return job
            recovered = True
            for nid in waiting_resources:
                if not await resource_coordinator.write_coordination_available(
                    node_by_id[nid].resource_claims
                ):
                    recovered = False
                    break
            if recovered:
                if ensure_active_capacity is not None and not await ensure_active_capacity(job):
                    # The task is no longer admitted.  Do not resume a write
                    # just because Redis recovered while the user/global pool
                    # is full; keep it in the suspended wait pool.
                    await asyncio.sleep(1)
                    continue
                for nid in waiting_resources:
                    node = node_by_id[nid]
                    node.metadata = dict(node.metadata or {})
                    node.metadata.pop("waiting_resources", None)
                    node.error = None
                    node.error_code = None
                    node.status = TaskStatus.PENDING
                job.status = JobStatus.RUNNING
                job.routing = dict(job.routing or {})
                job.routing.pop("waiting_resources_started_at", None)
                await store.save_job(job)
                waiting_resources = set()

        decision = decide_next_nodes(
            node_by_id,
            pending_ids=pending,
            completed_ids=completed,
            settled_ids=settled,
            waiting_resource_ids=waiting_resources,
        )
        if decision.skip_ids:
            for nid in decision.skip_ids:
                node = node_by_id[nid]
                node.status = TaskStatus.SKIPPED
                node.error = "前置依赖失败"
                node.completed_at = time.time()
                pending.discard(nid)
            await store.save_job(job)

        # 调度所有依赖已完成的步骤；它们只受 semaphore 限流，不形成批次屏障。
        # 清单节点显式声明失败可继续，因此以终态而非成功态解除其串行依赖。
        for nid in decision.ready_ids:
            node = node_by_id[nid]
            node.status = TaskStatus.READY
            await store.save_job(job)
            running_by_id[nid] = asyncio.create_task(_with_sem(node))
            pending.discard(nid)

        if not running_by_id:
            if waiting_resources:
                # Keep the execution loop alive without holding a semaphore or
                # channel lease. The next iteration probes Redis again.
                await asyncio.sleep(1)
                continue
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
            elif (
                node_by_id[nid].status == TaskStatus.PENDING
                and bool((node_by_id[nid].metadata or {}).get("waiting_resources"))
            ):
                # The node was deliberately unscheduled before a worker or
                # channel slot was touched. Put the same node back in the
                # dependency-driven queue for a later coordination probe.
                pending.add(nid)

    # ── 汇总任务状态 ──
    job = await store.get_job(job.job_id) or job
    statuses = [n.status for n in job.nodes]
    if job.status not in (
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
        JobStatus.PAUSED,
        JobStatus.WAITING_RESOURCES,
    ):
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
