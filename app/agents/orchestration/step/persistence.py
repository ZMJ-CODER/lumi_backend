"""Step 持久化：结果引用与检查点（**顺序即铁律**）。

两条不变量：

1. **完成事件必须在检查点落盘之后**：这里只写检查点、不发事件；发射侧的闸门是
   :func:`app.services.step_checkpoint.StepCheckpointCoordinator.confirm_persisted_for_emit`；
2. **写失败不阻塞执行**：结果引用与检查点写失败都只记日志——任务状态以 Job 快照为准，
   恢复时按"没有检查点"保守处理（宁可少恢复，不可错恢复）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from lumi_execution.step_contract import StepRunState

from app.agents.orchestration.models import Job
from app.agents.orchestration.step.state_adapter import (
    _current_step_id,
    _effect_type_for_node,
)


async def _persist_node_result_ref(
    user_id: str,
    result: dict | None,
    *,
    job_id: str = "",
    step_id: str = "",
    tool_name: str = "",
    schema_name: str = "execution_result",
    schema_version: int = 1,
) -> dict[str, str] | None:
    try:
        from app.agents.orchestration.execution.lineage import persist_result_ref

        return await persist_result_ref(
            user_id,
            result,
            job_id=job_id,
            step_id=step_id,
            tool_name=tool_name,
            schema_name=schema_name,
            schema_version=schema_version,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("步骤结果引用持久化失败（降级继续）: {}", str(exc)[:160])
        return None


async def _record_step_checkpoints(job: Job, state: StepRunState) -> None:
    """方案 §2.3 第 6/7 步：**结果引用已落盘、状态已写 Job 之后**写步骤检查点。

    这里只写检查点，不发事件：完成事件由内核对**已保存的状态**发射，而
    ``save_state`` 一定在发射之前被调用（见 ``lumi_execution.step_engine._settle``），
    因此"完成事件必须在检查点落盘之后"这条铁律由调用顺序保证；发射侧的闸门是
    :func:`app.services.step_checkpoint.StepCheckpointCoordinator.confirm_persisted_for_emit`。

    检查点写失败**不阻塞任务执行**（只记日志）：任务状态仍以 Job 快照为准，
    恢复时按"没有检查点"保守处理。
    """
    from app.services.step_checkpoint import coordinator_for, state_for_step

    coordinator = coordinator_for(job.job_id)
    if not coordinator.enabled:
        return
    job_status = job.status.value if hasattr(job.status, "value") else str(job.status)
    nodes_by_id = {node.id: node for node in job.nodes}
    written: list[Any] = []
    for raw in state.steps:
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or raw.get("step_id") or "")
        if not step_id:
            continue
        node = nodes_by_id.get(step_id)
        metadata = node.metadata if node is not None and isinstance(node.metadata, dict) else {}
        effect_type = str(
            raw.get("effect_type")
            or metadata.get("effect_type")
            or _effect_type_for_node(node)
        )
        outcome = await coordinator.record(
            step_id,
            state_for_step(status=str(raw.get("status") or ""), job_status=job_status),
            attempt=int(raw.get("attempt") or 1),
            tool_name=str(raw.get("tool") or raw.get("tool_name") or "")[:160],
            step_type=str(raw.get("step_type") or "")[:80],
            effect_type=effect_type[:32],
            idempotency_key=str(getattr(node, "idempotency_key", "") or "")[:160],
            input_digest=str(metadata.get("input_sha256") or raw.get("input_digest") or "")[:128],
            output_summary=str(raw.get("result_summary") or "")[:2000],
            result_ref=raw.get("result_ref") if isinstance(raw.get("result_ref"), dict) else None,
            error_code=str(raw.get("error_code") or (getattr(node, "error_code", "") or ""))[:120],
            effect_status=str(
                raw.get("effect_status") or (getattr(node, "effect_status", "") or "")
            ),
        )
        if outcome.checkpoint is not None:
            written.append(outcome.checkpoint)
    if not written:
        return
    # 方案 §3.3：检查点同时异步投影进 DB（查询/审计用）。DB 写失败不阻塞执行。
    from app.services.job_projection import project_job_run, project_step_checkpoints

    await project_step_checkpoints(job.job_id, written)
    await project_job_run(
        job_id=job.job_id,
        user_id=str(job.user_id or ""),
        conversation_id=str(getattr(job, "conversation_id", "") or ""),
        status=job_status,
        current_step_id=_current_step_id(state),
        plan_revision=int(state.plan_revision or 1),
        last_checkpoint_version=max(int(item.checkpoint_version or 0) for item in written),
        error_code=str(state.error or ""),
    )


__all__ = ["_persist_node_result_ref", "_record_step_checkpoints"]
