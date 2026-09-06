"""任务执行引擎使用的持久化状态适配器。"""

from __future__ import annotations

import time
from typing import Any

from lumi_execution import NodeExecutionResult
from lumi_orch.job_spec import JobSpec, NodeSpec

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus


TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
    TaskStatus.INTERRUPTED,
    TaskStatus.CANCELLED,
    TaskStatus.ESCALATED,
})


def prior_node_result(node: TaskNode) -> NodeExecutionResult:
    """Convert a durable terminal node snapshot into a core engine result."""
    status = "interrupted" if node.status == TaskStatus.CANCELLED else node.status.value
    return NodeExecutionResult(
        node_id=node.id,
        status=status,  # type: ignore[arg-type]
        result=node.result,
        error=node.error,
        error_code=node.error_code,
        retries=node.retries,
        effect_status=node.effect_status,
    )


class ApplicationExecutionControl:
    """Reads durable user pause/cancel decisions through the state-store port."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def get_status(self, job_id: str) -> str:
        job = await self._store.get_job(job_id)
        return job.status.value if job is not None else "interrupted"


class ApplicationNodeLifecycle:
    """Persist state transitions emitted by the runtime-neutral scheduler."""

    def __init__(self, job: Job, store: Any) -> None:
        self._job = job
        self._store = store
        self._nodes = {node.id: node for node in job.nodes}
        self._started: set[str] = set()

    async def on_node_state(
        self,
        _spec: JobSpec,
        spec_node: NodeSpec,
        phase: str,
        result: NodeExecutionResult | None,
    ) -> None:
        node = self._nodes[spec_node.id]
        if phase == "ready":
            node.status = TaskStatus.READY
        elif phase == "running":
            node.status = TaskStatus.RUNNING
            node.started_at = node.started_at or time.time()
            self._started.add(node.id)
        elif result is not None:
            self._apply_result(node, result)
        await self._store.save_job(self._job)

    def _apply_result(self, node: TaskNode, result: NodeExecutionResult) -> None:
        # DecisionNode is an orchestrator control signal, not user work.  Keep
        # its structured decision in the durable snapshot and project
        # clarification/domain requests into job-level state so they are not
        # mistaken for an ordinary tool success.
        if node.is_decision_node and result.status == "completed" and isinstance(result.result, dict):
            decision = str(result.result.get("decision") or "").strip().casefold()
            if decision == "clarify":
                node.status = TaskStatus.COMPLETED
                node.result = result.result
                node.error = node.error_code = None
                node.completed_at = time.time()
                self._job.status = JobStatus.COMPLETED
                self._job.result = {
                    "type": "clarification",
                    "question": str(result.result.get("content") or result.result.get("output") or "完成任务还需要补充信息。"),
                    "source": "decision_node",
                    "node_id": node.id,
                }
                return
            if decision == "request_domain":
                routing = dict(self._job.routing or {})
                routing["active_domain"] = str(result.result.get("domain") or "")
                routing["last_decision"] = {"decision": decision, "node_id": node.id}
                self._job.routing = routing
        if result.status == "waiting_approval":
            # ApprovalService validates the exact tool and argument
            # fingerprint from the durable node snapshot.  Keep the pending
            # worker payload on the node; dropping it here turns a valid
            # approval signal into an unrecoverable "missing credentials"
            # state after the execution engine suspends.
            node.result = result.result
            node.error = result.error
            node.error_code = result.error_code
            node.retries = result.retries
            node.effect_status = result.effect_status
            node.status = TaskStatus.ESCALATED
            self._job.status = JobStatus.WAITING_APPROVAL
        elif result.status == "waiting_resources":
            node.status = TaskStatus.PENDING
            node.metadata = {**(node.metadata or {}), "waiting_resources": True}
            node.error = result.error
            node.error_code = result.error_code
            self._job.status = JobStatus.WAITING_RESOURCES
        elif result.status == "interrupted" and self._job.status == JobStatus.CANCELLED:
            node.status = TaskStatus.INTERRUPTED if node.id in self._started else TaskStatus.CANCELLED
            node.error = result.error
            node.error_code = result.error_code
        else:
            node.status = TaskStatus(result.status)
            node.result = result.result
            node.error = result.error
            node.error_code = result.error_code
            node.retries = result.retries
            node.effect_status = result.effect_status
        if result.status not in {"waiting_approval", "waiting_resources"}:
            node.completed_at = time.time()
