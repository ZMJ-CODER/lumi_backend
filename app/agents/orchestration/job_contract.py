"""可变应用 Job 与冻结内核任务规格之间的适配器。"""

from __future__ import annotations

from lumi_orch.job_spec import JobSnapshot, JobSpec, NodeExecutionSpec, NodeResult, NodeSpec

from app.agents.orchestration.models import Job
from app.agents.orchestration.policy.execution_defaults import resolve_node_execution_spec


def freeze_job_spec(job: Job) -> JobSpec:
    """Freeze a validated Job before a backend receives it.

    ``Job`` continues to hold mutable execution state.  The immutable spec is
    retained in routing for audit and is the only plan-shaped payload sent to
    a Temporal Workflow.
    """
    task_policy = dict((job.routing or {}).get("task_policy") or {})
    frozen_nodes: list[NodeSpec] = []
    policy_snapshots: dict[str, dict] = {}
    for node in job.nodes:
        declared = (node.metadata or {}).get("execution_spec")
        base = NodeSpec(
            id=node.id,
            agent=node.agent,
            name=node.name,
            params=node.params,
            depends_on=tuple(node.depends_on),
            resource_claims=tuple(node.resource_claims),
            idempotency_key=node.idempotency_key,
            approval=node.approval,
            approval_note=node.approval_note,
            max_retries=node.max_retries,
            execution=(NodeExecutionSpec.model_validate(declared) if isinstance(declared, dict) else node.execution),
            metadata=node.metadata,
        )
        execution, snapshot = resolve_node_execution_spec(base.execution, task_policy=task_policy)
        frozen_nodes.append(base.model_copy(update={"execution": execution}))
        policy_snapshots[node.id] = snapshot
    spec = JobSpec(
        job_id=job.job_id,
        user_id=job.user_id,
        user_role=job.user_role,
        scene=job.scene,
        request=job.request,
        routing={
            key: value
            for key, value in (job.routing or {}).items()
            if key not in {"runtime", "temporal_submit_error", "execution_spec"}
        } | {"policy_snapshot": {"nodes": policy_snapshots}},
        nodes=tuple(frozen_nodes),
    ).with_fingerprint()
    job.routing = {
        **(job.routing or {}),
        "execution_spec": {
            "version": spec.version,
            "fingerprint": spec.fingerprint,
            "policy_snapshots": policy_snapshots,
        },
    }
    return spec


def snapshot_from_job(job: Job) -> JobSnapshot:
    """Expose a portable query response without leaking scheduler internals."""
    return JobSnapshot(
        job_id=job.job_id,
        status=job.status.value,
        node_results=tuple(
            NodeResult(
                node_id=node.id,
                status=node.status.value,
                result=node.result,
                error=node.error,
                error_code=node.error_code,
                retries=node.retries,
                effect_status=node.effect_status,
            )
            for node in job.nodes
            if node.status.value in {"completed", "failed", "skipped", "interrupted", "escalated"}
        ),
        result=job.result,
        error=job.error,
    )
