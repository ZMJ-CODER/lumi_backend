"""_finalize_logical_answer _wait_for_ready_expansion（logical_read_activities 的 helpers 族）。"""

from __future__ import annotations
import time
from app.agents.orchestration.models import JobStatus


async def _finalize_logical_answer(job, plan: dict) -> None:
    """从不透明的逻辑计划结果引用生成最终交付。"""
    if job.result:
        return
    records = plan.get("nodes") or {}
    sources = []
    for node_id in plan.get("order") or []:
        record = records.get(node_id) or {}
        if str(record.get("status") or "") != "completed":
            continue
        node = record.get("node") or {}
        ref = record.get("result_ref")
        if isinstance(ref, dict):
            sources.append(
                {
                    "agent": str(node.get("agent") or ""),
                    "title": str(node.get("name") or node.get("agent") or node_id),
                    "result_ref": ref,
                }
            )
    if len(sources) == 1:
        # 单节点结果可由普通响应路径解析并渲染，无需额外调用 LLM 改写。
        return
    if not sources:
        return
    from app.agents.orchestration.temporal.activities import synthesize_final_answer_activity

    out = await synthesize_final_answer_activity(
        {
            "user_id": job.user_id,
            "job_id": job.job_id,
            "request": job.request,
            "nodes": sources,
        }
    )
    if isinstance(out, dict) and out.get("final_answer"):
        job.result = out


async def _wait_for_ready_expansion(store, job, plan: dict, pointer: dict) -> dict | None:
    """将已完成当前节点图但仍有就绪插槽的任务转为调度等待态。"""
    from app.agents.orchestration.planning.logical_plan import logical_plan_progress, save_logical_plan
    from app.agents.orchestration.scheduling.plan_patches import ready_slots

    slots = ready_slots(plan)
    if not slots:
        return None
    job.status = JobStatus.PAUSED
    job.error = None
    job.updated_at = time.time()
    job.routing = {
        **(job.routing or {}),
        "logical_plan": {
            **pointer,
            "revision": plan.get("revision", 1),
            "progress": logical_plan_progress(plan),
            "used_estimated_tokens": (plan.get("budget") or {}).get("used_estimated", 0),
        },
        "scheduler_waiting_slots": [slot.id for slot in slots],
    }
    await save_logical_plan(job.user_id, plan)
    await store.save_job(job)
    return {
        "terminal": False,
        "waiting_expansion": True,
        "status": job.status.value,
        "slot_ids": [slot.id for slot in slots],
    }
