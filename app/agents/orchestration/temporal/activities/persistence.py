"""persist_node_result_ref_activity（activities 的 persistence 族）。"""

from temporalio import activity


@activity.defn
async def persist_node_result_ref_activity(payload: dict) -> dict | None:
    """Persist a sanitized output and return its opaque reference for long DAGs."""
    from app.agents.orchestration.execution.lineage import persist_result_ref

    user_id = str(payload.get("user_id") or "")
    result = payload.get("result")
    return await persist_result_ref(user_id, result if isinstance(result, dict) else None)
