"""replan_static_job_activity（activities 的 replan 族）。"""

import hashlib
import json
from temporalio import activity
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.execution.workers import WORKERS
from app.agents.orchestration.temporal.client import load_job_llm_config
from app.core.config import settings


@activity.defn
async def replan_static_job_activity(payload: dict) -> dict:
    """Create one pure-read replacement ``JobSpec`` outside Workflow replay.

    The Workflow decides whether recovery is permitted and only validates then
    mounts this returned spec.  Model calls, Redis context reads and plan
    compilation all stay inside the Activity boundary.
    """
    from lumi_orch.job_spec import JobSpec, NodeSpec

    from app.agents.orchestration.planning.compilation import PlanCompilationService
    from app.agents.orchestration.planning.context import PlanRequestContext
    from app.agents.orchestration.planning.planner import LlmPlanner
    from app.agents.orchestration.planning.tca import ComplexityLevel
    from app.agents.orchestration.temporal.client import (
        load_temporal_replan_context,
    )
    from app.agents.orchestration.runtime.temporal_policy import evaluate_static_temporal_nodes
    from app.agents.orchestration.execution.safety import is_effectful, prepare_node_safety

    job_id = str(payload.get("job_id") or "")
    old_spec_raw = payload.get("execution_spec") or {}
    try:
        old_spec = JobSpec.model_validate(old_spec_raw)
    except Exception:
        return {"allowed": False, "reason": "invalid_execution_spec"}
    if any(node.approval or node.idempotency_key for node in old_spec.nodes):
        return {"allowed": False, "reason": "effectful_or_approval_job"}

    context_data = await load_temporal_replan_context(job_id)
    if not context_data:
        return {"allowed": False, "reason": "context_unavailable"}
    llm_config = await load_job_llm_config(job_id)
    current_nodes = payload.get("nodes") or []
    completed_ids = [
        str(node.get("id")) for node in current_nodes
        if str(node.get("status") or "") == "completed"
    ]
    failure_lines = [
        {
            "node": str(node.get("name") or node.get("agent") or node.get("id")),
            "error_code": str(node.get("error_code") or ""),
            "error": str(node.get("error") or "")[:500],
        }
        for node in current_nodes
        if str(node.get("status") or "") in {"failed", "escalated"}
    ]
    prior = str(context_data.get("prior_summaries") or "")
    prior += "\n\n[Temporal 静态任务失败反馈]\n" + json.dumps(
        failure_lines, ensure_ascii=False, default=str
    )
    context = PlanRequestContext.from_mapping(context_data).with_llm_config(llm_config).with_prior_summaries(prior)
    planner = LlmPlanner()
    try:
        tree = await planner.plan_for_level(
            ComplexityLevel.M2,
            context=context,
            bypass_fast_paths=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"allowed": False, "reason": "planner_error", "error": str(exc)[:300]}
    if tree.error or not tree.nodes:
        return {"allowed": False, "reason": "replan_empty", "error": str(tree.error or tree.clarification or "")[:300]}

    compiler = PlanCompilationService(
        workers=WORKERS,
        plan_with_context=planner.plan_context,
        temporal_static_mode=True,
    )
    try:
        tree = await compiler.compile_with_feedback(
            tree,
            routing=dict(context_data.get("routing") or {}),
            context=context,
            user_role=str(payload.get("user_role") or "user"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"allowed": False, "reason": "compiler_error", "error": str(exc)[:300]}
    if tree.error or not tree.nodes:
        return {"allowed": False, "reason": "replan_rejected", "error": str(tree.error or "")[:300]}

    revision = int((context_data.get("routing") or {}).get("plan_revision") or 1) + 1
    replacement_nodes: list[TaskNode] = []
    for index, node in enumerate(tree.nodes, start=1):
        node.id = f"temporal-replan-{revision}-{index}-{hashlib.sha256((job_id + node.id).encode()).hexdigest()[:8]}"
        if not node.depends_on:
            node.depends_on = list(completed_ids)
        node.metadata = {**(node.metadata or {}), "plan_revision": revision, "temporal_replan": True}
        prepare_node_safety(node, old_spec.user_id, job_id)
        if node.approval or is_effectful(node):
            return {"allowed": False, "reason": "replacement_not_pure_read"}
        replacement_nodes.append(node)

    decision = evaluate_static_temporal_nodes(
        replacement_nodes,
        max_nodes=max(1, int(settings.TEMPORAL_STATIC_MAX_NODES)),
    )
    if not decision.eligible:
        return {"allowed": False, "reason": f"replacement_{decision.code}", "error": decision.detail}
    new_spec = JobSpec(
        job_id=old_spec.job_id,
        user_id=old_spec.user_id,
        user_role=old_spec.user_role,
        scene=old_spec.scene,
        request=old_spec.request,
        routing={
            **old_spec.routing,
            "plan_revision": revision,
            "replan_count": int(old_spec.routing.get("replan_count") or 0) + 1,
        },
        nodes=tuple(
            [*old_spec.nodes] + [
                NodeSpec(
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
                    metadata=node.metadata,
                )
                for node in replacement_nodes
            ]
        ),
    ).with_fingerprint()
    return {
        "allowed": True,
        "execution_spec": new_spec.model_dump(mode="json"),
        "replacement_node_ids": [node.id for node in replacement_nodes],
        "plan_text": str(tree.plan_text or ""),
        "revision": revision,
    }
