"""滚动任务清单 Temporal 工作流使用的活动。

一个 Activity 负责一个持久化批次。它复用既有 DAG 节点运行时、锁与副作用日
志；Temporal 则通过 Continue-As-New 提供进程隔离、心跳恢复与历史压缩。
"""

from __future__ import annotations

import asyncio

from temporalio import activity

from app.agents.orchestration.models import JobStatus
from app.agents.orchestration.state import RedisStateStore
from app.agents.orchestration.workers import WORKERS
from app.core.config import settings


def _heartbeat(details: dict) -> None:
    try:
        activity.heartbeat(details)
    except RuntimeError:
        # Allows narrow unit tests to invoke the implementation without an
        # Activity execution context.
        pass


@activity.defn
async def run_manifest_batch_activity(payload: dict) -> dict:
    """Run exactly one materialized manifest window and persist its outcome."""
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return {"terminal": True, "status": "failed", "error": "missing job_id"}

    from app.agents.orchestration.admission import job_admission
    from app.agents.orchestration.execution.validation import execute_dag
    from app.agents.orchestration.orchestrator import AgentOrchestrator
    from app.agents.orchestration.review import get_reviewer
    from app.agents.orchestration.temporal.client import load_job_llm_config

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None:
        return {"terminal": True, "status": "failed", "error": "job_not_found"}
    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
        return {"terminal": True, "status": job.status.value}

    interval = max(5.0, float(settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS))
    stopped = asyncio.Event()

    async def heartbeat_loop() -> None:
        while not stopped.is_set():
            _heartbeat({"job_id": job_id, "phase": "manifest_batch"})
            # The admission slot has independent ownership from Temporal. Keep
            # it alive while a worker owns this batch, including after API
            # process restarts.
            await job_admission.renew(job_id, job.user_id)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
            except TimeoutError:
                pass

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        llm_config = await load_job_llm_config(job_id)
        api_key = (llm_config or {}).get("api_key")
        await execute_dag(
            job,
            WORKERS,
            get_reviewer(),
            store,
            concurrency=settings.AGENT_NODE_CONCURRENCY,
            llm_api_key=api_key,
            llm_config=llm_config,
        )
        job = await store.get_job(job_id)
        if job is None:
            return {"terminal": True, "status": "failed", "error": "job_lost"}
        # The API can cancel a job while this activity is winding down.  The
        # persisted control state wins over this activity's stale snapshot:
        # never advance a manifest cursor or synthesize a final result after a
        # user has already ended the task.
        if job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.PAUSED,
        }:
            return {"terminal": job.status != JobStatus.PAUSED, "status": job.status.value}

        # The existing manifest controller is deliberately reused here. It is
        # the only component that commits cursor movement, route upgrades and
        # collection semantics, so a retry cannot invent a second protocol.
        controller = AgentOrchestrator(
            store=store,
            workers=WORKERS,
            review=get_reviewer(),
            temporal_enabled=False,
        )
        if llm_config:
            controller._job_plan_context[job_id] = {
                "llm_api_key": api_key,
                "llm_config": llm_config,
            }
        advance = await controller._continue_manifest_job(job)
        job = await store.get_job(job_id) or job
        terminal = job.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
        return {
            "terminal": terminal,
            "continue": bool(advance),
            "status": job.status.value,
            "cursor": int((job.routing.get("manifest_progress") or {}).get("cursor") or 0),
        }
    finally:
        stopped.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


@activity.defn
async def fail_manifest_job_activity(payload: dict) -> None:
    """Converge Redis state after Temporal has exhausted an Activity retry.

    Workflow failure alone is not visible to the existing SSE/API contract,
    whose source of truth remains Redis. This compensating activity respects
    an intervening user cancellation or pause.
    """
    job_id = str((payload or {}).get("job_id") or "")
    if not job_id:
        return
    error = str((payload or {}).get("error") or "清单批次执行失败")[:500]

    from app.agents.orchestration.admission import job_admission

    store = RedisStateStore()
    job = await store.get_job(job_id)
    if job is None or job.status in {
        JobStatus.COMPLETED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
        JobStatus.PAUSED,
    }:
        return
    job.status = JobStatus.FAILED
    job.error = error
    await store.save_job(job)
    await job_admission.release(job_id=job.job_id, user_id=job.user_id)
