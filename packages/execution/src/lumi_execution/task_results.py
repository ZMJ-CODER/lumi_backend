"""可跨运行时使用的任务执行结果契约与聚合辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from lumi_orch.job_spec import JobSpec, NodeResult


NodeExecutionStatus = Literal[
    "completed", "failed", "skipped", "interrupted", "escalated",
    "waiting_approval", "waiting_resources",
]
JobExecutionStatus = Literal[
    "completed", "partial", "degraded", "failed", "cancelled", "interrupted", "paused",
    "waiting_approval", "waiting_resources",
]

TERMINAL_NODE_STATUSES = frozenset({"completed", "failed", "skipped", "interrupted", "escalated"})
FAILED_DEPENDENCY_STATUSES = frozenset({"failed", "skipped", "interrupted", "escalated"})
STOPPED_JOB_STATUSES = frozenset({"waiting_approval", "waiting_resources"})


class NodeExecutionResult(BaseModel):
    """Result returned by an application node adapter."""

    node_id: str
    status: NodeExecutionStatus
    result: dict | None = None
    error: str | None = None
    error_code: str | None = None
    retries: int = Field(default=0, ge=0)
    effect_status: str | None = None


class JobExecutionResult(BaseModel):
    """Portable, fully converged or explicitly suspended task outcome."""

    job_id: str
    status: JobExecutionStatus
    node_results: tuple[NodeExecutionResult, ...]
    result: dict | None = None
    error: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, object] = Field(default_factory=dict)
    failures: tuple[dict[str, object], ...] = ()
    trace_id: str | None = None
    policy_snapshot: dict[str, object] = Field(default_factory=dict)

    def snapshot_results(self) -> tuple[NodeResult, ...]:
        return tuple(
            NodeResult(
                node_id=node.node_id,
                status=node.status,
                result=node.result,
                error=node.error,
                error_code=node.error_code,
                retries=node.retries,
                effect_status=node.effect_status,
            )
            for node in self.node_results
            if node.status in TERMINAL_NODE_STATUSES
        )


def build_job_result(
    spec: JobSpec,
    results: Mapping[str, NodeExecutionResult],
    status: JobExecutionStatus | str,
) -> JobExecutionResult:
    """Return ordered node outputs and one runtime-neutral task result."""
    allowed = {
        "completed", "partial", "degraded", "failed", "cancelled", "interrupted", "paused",
        "waiting_approval", "waiting_resources",
    }
    normalized: JobExecutionStatus = status if status in allowed else "failed"  # type: ignore[assignment]
    ordered = tuple(results[node.id] for node in spec.nodes if node.id in results)
    error = next((node.error for node in ordered if node.error), None)
    outputs = {
        node.node_id: node.result
        for node in ordered
        if node.status == "completed" and node.result is not None
    }
    failures = tuple(
        {
            "node": node.node_id,
            "error": node.error,
            "error_code": node.error_code,
            "retry_exhausted": node.status == "failed",
        }
        for node in ordered if node.status in FAILED_DEPENDENCY_STATUSES
    )
    return JobExecutionResult(
        job_id=spec.job_id,
        status=normalized,
        node_results=ordered,
        result={
            "outputs": outputs,
            "completed_node_ids": [node.node_id for node in ordered if node.status == "completed"],
            "failed_node_ids": [node.node_id for node in ordered if node.status in FAILED_DEPENDENCY_STATUSES],
        },
        error=error,
        failures=failures,
        policy_snapshot=dict(spec.routing.get("policy_snapshot") or {}),
    )
