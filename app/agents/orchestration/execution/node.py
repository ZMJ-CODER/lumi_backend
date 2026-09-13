"""单个任务节点的工作节点、资源与副作用日志适配器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from lumi_execution import NodeExecutionResult
from lumi_orch.job_spec import JobSpec, NodeSpec
from lumi_orch.resources import ResourceCoordinator

from app.agents.orchestration.models import Job, TaskNode, TaskStatus


class ApplicationTaskNodeExecutor:
    """Run one frozen node behind Lumi's durability and safety boundaries."""

    def __init__(
        self,
        *,
        job: Job,
        workers: Mapping[str, Any],
        review: Any,
        store: Any,
        llm_api_key: str | None,
        llm_config: dict | None,
    ) -> None:
        self._job = job
        self._workers = workers
        self._review = review
        self._store = store
        self._llm_api_key = llm_api_key
        self._llm_config = llm_config
        self._nodes = {node.id: node for node in job.nodes}
        self._local_coordinator = ResourceCoordinator(fail_closed=lambda _claim: False)

    async def execute_node(
        self,
        _spec: JobSpec,
        spec_node: NodeSpec,
        dependency_results: Mapping[str, dict],
    ) -> NodeExecutionResult:
        from app.agents.orchestration.policy.runtime import node_timeout_seconds
        from app.agents.orchestration.safety import is_effectful, prepare_node_safety

        node = self._nodes[spec_node.id]
        worker = self._workers.get(node.agent)
        if worker is None:
            return self._failure(node, "未注册的执行 agent: " + node.agent, "AGENT_NOT_FOUND")
        # 能力门禁（阶段 4/3）：节点显式声明了能力时，在**执行前**拦下静态不可能的情况
        # （未声明能力/位置违反数据本地性）；运行时不可用只记录，交给 Broker 在调用点
        # 给出准确错误。默认关闭，开启后行为对未声明能力的节点完全不变。
        gate_failure = self._capability_gate_failure(node)
        if gate_failure is not None:
            return gate_failure
        prepare_node_safety(node, self._job.user_id, self._job.job_id)
        # Forked prefixes intentionally keep no result body in the branch Job.
        # Resolve their owner-scoped references at execution time so the core
        # engine can still pass the same dependency contract to the Worker.
        from app.agents.orchestration.context import build_dependency_context_from_refs

        resolved_dependencies = await build_dependency_context_from_refs(
            node,
            self._nodes,
            user_id=self._job.user_id,
        )
        # The core passes ``{}`` for an already-completed fork prefix because
        # its body was intentionally removed from the frozen JobSpec. Never
        # overwrite a body resolved from the node's reference with that empty
        # placeholder. Normal in-memory execution still uses the core result.
        merged_dependencies = dict(dependency_results)
        merged_dependencies.update(resolved_dependencies)
        node.metadata = {**(node.metadata or {}), "dependency_results": merged_dependencies}
        node.metadata.setdefault("tool_index", 0)
        worker_node = await self._prepare_worker_node(node)
        # The typed spec is the engine contract; legacy safety detection remains
        # as a compatibility guard for jobs created before the field existed.
        effectful = is_effectful(node) or spec_node.execution.requires_effect_journal()
        timeout_seconds = spec_node.execution.timeout_seconds or node_timeout_seconds(node)
        policy_attempts = spec_node.execution.retry.max_attempts
        max_retries = max(0, int(policy_attempts) - 1) if policy_attempts is not None else node.max_retries
        coordinator, claims = self._resource_scope(node)
        if not await coordinator.write_coordination_available(claims):
            return self._waiting_resources(node)

        prior = await self._reserve_effect(node, effectful)
        if prior is not None:
            return prior
        try:
            outcome = await self._run_worker(
                coordinator=coordinator,
                claims=claims,
                worker=worker,
                node=node,
                worker_node=worker_node,
                effectful=effectful,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
        except asyncio.CancelledError:
            await self._mark_effect_uncertain(node, effectful, "task_cancelled")
            raise
        if outcome is None:
            await self._abandon_effect(node, effectful)
            return self._waiting_resources(node)
        return await self._persist_outcome(node, outcome, effectful)

    def _resource_scope(self, node: TaskNode) -> tuple[Any, list[Any]]:
        from app.agents.orchestration.resources import resource_coordinator

        # AgentOrchestrator wraps every StateStore in StateStoreJobRepository.
        # Inspect the wrapped store as well, otherwise in-memory tests are
        # mistaken for production Redis and write nodes wait forever when
        # Redis is intentionally absent.
        backing_store = getattr(self._store, "_store", self._store)
        coordinator = (
            self._local_coordinator
            if backing_store.__class__.__name__ == "InMemoryStateStore"
            else resource_coordinator
        )
        return coordinator, list(node.resource_claims)

    async def resources_available(self, nodes: list[TaskNode]) -> bool:
        """Probe every suspended write claim before the service resumes a DAG."""
        for node in nodes:
            coordinator, claims = self._resource_scope(node)
            if not await coordinator.write_coordination_available(claims):
                return False
        return True

    async def _prepare_worker_node(self, node: TaskNode) -> TaskNode:
        """为汇总 Worker 解析滚动窗口外的结果引用。

        只读结果正文仅存在本次 Worker 的副本中，不写回 Job 的 params 或
        Event History；持久化任务继续只保存 result_ref。
        """
        entries = (node.metadata or {}).get("logical_collection_refs")
        if node.agent != "collect_results" or not isinstance(entries, list):
            return node

        from app.agents.orchestration.execution.lineage import resolve_result_ref

        items: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get("node_id") or "")
            result = await resolve_result_ref(self._job.user_id, entry.get("result_ref"))
            item = {
                "id": node_id,
                "title": str(entry.get("title") or node_id)[:240],
                "status": str(entry.get("status") or "unknown"),
            }
            if isinstance(result, dict):
                item["result"] = result
            else:
                item["status"] = "unavailable"
                item["error"] = "前序结果引用不可用"
            items.append(item)

        worker_node = node.model_copy(deep=True)
        worker_node.params = {**(worker_node.params or {}), "items": items}
        return worker_node

    async def _run_worker(self, *, coordinator, claims, worker, node, worker_node, effectful, timeout_seconds, max_retries):
        from app.agents.orchestration.channel_limits import channel_limiter
        from app.agents.orchestration.execution.node_runtime import NodeExecutionRunner
        from app.agents.orchestration.resources import WriteResourceCoordinationUnavailable
        from app.agents.orchestration.execution.telemetry import LumiExecutionTelemetry

        async def on_running(_attempt: int) -> None:
            node.status = TaskStatus.RUNNING
            node.started_at = node.started_at or time.time()
            node.error = node.error_code = None
            await self._store.save_job(self._job)

        async def on_retry(attempt: int) -> None:
            node.retries = attempt
            node.status = TaskStatus.RETRYING
            await self._store.save_job(self._job)

        try:
            channel = "node_execution"
            # 所有会触发文本模型的节点共享 Provider 级闸门。DAG 并发上限
            # 只限制节点数量，不能代替供应商的 RPM/连接池预算。
            if node.agent in {"direct_llm", "atomic_step", "react_step", "collect_results", "office_text", "office_research"}:
                channel = "llm_provider"
            async with channel_limiter.claim(channel, lease_seconds=max(60, timeout_seconds + 60)):
                async with coordinator.claim(claims, ttl=max(60, timeout_seconds + 60)):
                    return await NodeExecutionRunner(
                        worker=worker,
                        node=worker_node,
                        ctx=self._worker_context(node),
                        review=self._review,
                        timeout_seconds=timeout_seconds,
                        max_retries=0 if effectful else max_retries,
                        effectful=effectful,
                        on_running=on_running,
                        on_retry=on_retry,
                        telemetry=LumiExecutionTelemetry(),
                    ).run()
        except WriteResourceCoordinationUnavailable:
            return None

    def _worker_context(self, node: TaskNode) -> Any:
        from app.agents.orchestration.workers import WorkerContext
        from app.services.office_stream import push_delta

        async def on_output(text: str) -> None:
            await push_delta(self._job.job_id, node.id, text)

        return WorkerContext(
            user_id=self._job.user_id,
            job_id=self._job.job_id,
            scene=self._job.scene,
            user_role=self._job.user_role,
            llm_api_key=self._llm_api_key,
            llm_config=self._llm_config,
            user_request=self._job.request,
            confirmed_tools=frozenset(str(v) for v in (node.metadata or {}).get("confirmed_tools", [])),
            confirmed_tool_calls=frozenset(str(v) for v in (node.metadata or {}).get("confirmed_tool_calls", [])),
            approval_context_sha256=str((node.metadata or {}).get("approval_upstream_sha256") or ""),
            office_doc_ids=self._authorized_office_doc_ids(node),
            authorized_project_ids=self._authorized_project_ids(),
            workspace_id=self._authorized_workspace_id(),
            # 方案 4 §1.2：Worker 复用**同一份**画像事实（只读），不重新解析用户原文。
            task_profile=self._task_profile_facts(),
            on_output=on_output,
        )

    def _task_profile_facts(self) -> dict[str, Any]:
        """任务画像的只读事实（来自路由快照；缺字段时留空，绝不现场猜）。"""
        routing = self._job.routing if isinstance(self._job.routing, dict) else {}
        decision = routing.get("route_decision") if isinstance(routing.get("route_decision"), dict) else {}
        profile = decision.get("task_profile") if isinstance(decision.get("task_profile"), dict) else {}
        facts: dict[str, Any] = {}
        for key in (
            "action_intents",
            "target_scope",
            "target_clarity",
            "intent_type",
            "required_capabilities",
            "approval_required",
            "confidence_source",
            "decision_reason_code",
        ):
            value = profile.get(key, decision.get(key))
            if value not in (None, "", [], {}):
                facts[key] = value
        return facts

    def _authorized_project_ids(self) -> tuple[str, ...]:
        """返回提交请求显式授权的项目范围，不信任节点参数。"""
        routing = self._job.routing if isinstance(self._job.routing, dict) else {}
        values = []
        for value in routing.get("authorized_project_ids") or []:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
        return tuple(values)

    def _capability_gate_failure(self, node: TaskNode) -> "NodeExecutionResult | None":
        """执行前能力门禁（返回失败结果表示不要执行该节点）。

        开关默认关闭（``AGENT_CAPABILITY_GATE_ENABLED``），且只对**显式声明了能力**的
        节点生效：没有声明能力的节点在此完全不受影响，因此开启开关也不会改变既有
        计划的行为。只拦"静态不可能"的问题（未声明能力、位置违反本地性）。
        """
        try:
            from app.core.config import settings

            if not bool(getattr(settings, "AGENT_CAPABILITY_GATE_ENABLED", False)):
                return None
        except Exception:  # noqa: BLE001 - 配置不可用时按关闭处理（保守）
            return None
        from app.agents.capabilities.gate import declared_capabilities, node_capability_failure

        if not declared_capabilities(node):
            return None
        failure = node_capability_failure(node)
        if failure is None:
            return None
        error = failure.error
        return self._failure(
            node,
            str(getattr(error, "message", "") or "能力门禁未通过"),
            str(failure.error_code or "CAPABILITY_MISSING"),
        )

    def _authorized_workspace_id(self) -> str:
        """Return the request-scoped desktop workspace, never a node value."""
        routing = self._job.routing if isinstance(self._job.routing, dict) else {}
        return str(routing.get("workspace_id") or "").strip()

    def _authorized_office_doc_ids(self, node: TaskNode) -> tuple[str, ...]:
        """Use submission-scoped document refs; node params may only narrow.

        A planner/worker never gets to expand document access by putting a
        fresh ID in ``params``.  This mirrors the project scope rule above and
        fixes the former empty WorkerContext on normal document read nodes.
        """
        routing = self._job.routing if isinstance(self._job.routing, dict) else {}
        allowed: list[str] = []
        for item in routing.get("input_refs") or []:
            value = item.get("doc_id") if isinstance(item, dict) else item
            text = str(value or "").strip()
            if text and text not in allowed:
                allowed.append(text)
        requested = (node.params or {}).get("doc_ids")
        if isinstance(requested, str):
            requested = [requested]
        if isinstance(requested, (list, tuple, set)) and requested:
            requested_ids = {str(value).strip() for value in requested if str(value).strip()}
            return tuple(value for value in allowed if value in requested_ids)
        # Atomic ``read_document`` encodes its exact target in inputs.  It is
        # also only a narrowing hint and cannot authorize a foreign document.
        inputs = (node.params or {}).get("inputs")
        if isinstance(inputs, dict) and inputs.get("doc_id"):
            target = str(inputs.get("doc_id") or "").strip()
            return tuple(value for value in allowed if value == target)
        return tuple(allowed)

    async def _reserve_effect(self, node: TaskNode, effectful: bool) -> NodeExecutionResult | None:
        if not effectful or not node.idempotency_key:
            return None
        from app.agents.orchestration.effects import EffectJournalUnavailable, effect_guard, effect_intent_for_node

        try:
            existing = await effect_guard.reserve(
                node.idempotency_key, effect_intent_for_node(job_id=self._job.job_id, node=node)
            )
        except EffectJournalUnavailable:
            return self._failure(node, "副作用安全日志不可用，已阻止执行以避免重复操作", "EFFECT_JOURNAL_UNAVAILABLE", "pending")
        except RuntimeError:
            return self._failure(node, "副作用步骤已开始但结果不确定，已停止自动重试以避免重复执行", "EFFECT_UNCERTAIN", "uncertain")
        if existing is None:
            return None
        if existing is not None:
            return NodeExecutionResult(node_id=node.id, status="completed", result=(existing or {}).get("result"), effect_status="committed")
        return self._failure(node, "副作用步骤已开始但结果不确定，已停止自动重试以避免重复执行", "EFFECT_UNCERTAIN", "uncertain")

    async def _persist_outcome(self, node: TaskNode, outcome: Any, effectful: bool) -> NodeExecutionResult:
        from app.agents.orchestration.effects import EffectJournalUnavailable, effect_guard
        from app.agents.orchestration.presentation import attach_display_result

        if outcome.success:
            result = attach_display_result(node, outcome.result or {})
            effect_status = None
            if effectful and node.idempotency_key:
                try:
                    await effect_guard.confirm(node.idempotency_key, outcome.result)
                    effect_status = "committed"
                except EffectJournalUnavailable:
                    return self._failure(node, "副作用已执行但安全日志确认失败，已停止自动重试", "EFFECT_JOURNAL_UNAVAILABLE", "uncertain")
            return NodeExecutionResult(node_id=node.id, status="completed", result=result, retries=outcome.retries, effect_status=effect_status)

        if outcome.recovery:
            node.metadata["recovery"] = outcome.recovery
        if outcome.escalation:
            node.metadata["escalation"] = outcome.escalation
        if isinstance(outcome.result, dict) and isinstance(outcome.result.get("tool_metadata"), dict):
            node.metadata["tool_metadata"] = dict(outcome.result["tool_metadata"])
        if effectful and node.idempotency_key:
            if outcome.escalation and str(outcome.escalation.get("reason") or "") == "approval_required":
                try:
                    await effect_guard.abandon_pending(node.idempotency_key)
                except (EffectJournalUnavailable, RuntimeError):
                    return self._failure(node, "审批前副作用日志无法清理，已停止自动重试", "EFFECT_JOURNAL_UNAVAILABLE", "uncertain")
                return NodeExecutionResult(node_id=node.id, status="waiting_approval", result=outcome.result, error=outcome.error or "需要用户确认", error_code=outcome.error_code or "APPROVAL_REQUIRED", retries=outcome.retries, effect_status="pending")
            await self._mark_effect_uncertain(node, True, outcome.error_code or "execution_failed")
        return NodeExecutionResult(node_id=node.id, status="escalated" if outcome.escalation else "failed", result=outcome.result, error=outcome.error or "执行失败", error_code=outcome.error_code or "EXEC_ERROR", retries=outcome.retries, effect_status="uncertain" if effectful else None)

    async def _abandon_effect(self, node: TaskNode, effectful: bool) -> None:
        if effectful and node.idempotency_key:
            from app.agents.orchestration.effects import EffectJournalUnavailable, effect_guard
            try:
                await effect_guard.abandon_pending(node.idempotency_key)
            except (EffectJournalUnavailable, RuntimeError):
                pass

    async def _mark_effect_uncertain(self, node: TaskNode, effectful: bool, reason: str) -> None:
        if effectful and node.idempotency_key:
            from app.agents.orchestration.effects import EffectJournalUnavailable, effect_guard
            try:
                await effect_guard.mark_uncertain(node.idempotency_key, reason)
            except (EffectJournalUnavailable, RuntimeError):
                pass

    @staticmethod
    def _failure(node: TaskNode, error: str, code: str, effect_status: str | None = None) -> NodeExecutionResult:
        return NodeExecutionResult(node_id=node.id, status="failed", error=error, error_code=code, effect_status=effect_status)

    @staticmethod
    def _waiting_resources(node: TaskNode) -> NodeExecutionResult:
        return NodeExecutionResult(node_id=node.id, status="waiting_resources", error="写资源协调服务暂不可用，任务将自动等待恢复", error_code="RESOURCE_COORDINATION_UNAVAILABLE")
