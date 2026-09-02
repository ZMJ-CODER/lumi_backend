"""预声明审批副作用逻辑计划的 Temporal Workflow。

完整计划和 effect-journal 均在 Redis/PostgreSQL。Workflow 只保存当前任务引用、
生命周期控制与审批 Signal；写 Activity 继续使用既有 intent → confirm 保护。
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError


@workflow.defn
class LogicalEffectsWorkflow:
    """逐前沿推进已声明审批和幂等键的逻辑计划。"""

    def __init__(self) -> None:
        self._paused = False
        self._cancel_requested = False
        self._keep_completed = True
        self._approvals: list[dict] = []
        self._patch_pending = False
        # A persisted approval may be followed by an admission-capacity
        # pause before its Signal can be delivered.  Resume must therefore be
        # able to wake an approval wait even when there is no new Signal.
        self._wake_generation = 0

    @workflow.run
    async def run(self, payload: dict) -> dict:
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return {"status": "failed", "error": "missing job_id"}
        heartbeat_seconds = max(5, int(payload.get("heartbeat_seconds") or 15))
        frontier_timeout = max(60, int(payload.get("frontier_timeout_seconds") or 1800))
        continue_after = max(1, int(payload.get("continue_after_frontiers") or 20))
        completed_frontiers = max(0, int(payload.get("completed_frontiers") or 0))
        while True:
            if self._cancel_requested:
                await self._cancel(job_id, self._keep_completed)
                await self._cleanup(job_id)
                return {"status": "cancelled", "job_id": job_id}
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                continue
            task = asyncio.create_task(
                workflow.execute_activity(
                    "run_logical_effects_frontier_activity",
                    {"job_id": job_id, "approvals": list(self._approvals)},
                    start_to_close_timeout=timedelta(seconds=frontier_timeout),
                    heartbeat_timeout=timedelta(seconds=max(heartbeat_seconds * 2, heartbeat_seconds + 5)),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            )
            await workflow.wait_condition(lambda task=task: task.done() or self._paused or self._cancel_requested)
            if self._cancel_requested:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._cancel(job_id, self._keep_completed)
                await self._cleanup(job_id)
                return {"status": "cancelled", "job_id": job_id}
            if self._paused and not task.done():
                await task
            try:
                result = await task
            except CancelledError:
                await self._cleanup(job_id)
                return {"status": "cancelled", "job_id": job_id}
            except Exception as exc:
                await self._fail(job_id, str(exc))
                await self._cleanup(job_id)
                return {"status": "failed", "job_id": job_id, "error": str(exc)[:300]}
            self._consume_approval((result or {}).get("consumed_approval"))
            if bool((result or {}).get("waiting_approval")):
                wake_generation = self._wake_generation
                timeout_seconds = max(1, int((result or {}).get("approval_wait_seconds") or 60))
                try:
                    await workflow.wait_condition(
                        lambda wake_generation=wake_generation: (
                            bool(self._approvals)
                            or self._cancel_requested
                            or self._paused
                            or self._wake_generation != wake_generation
                        ),
                        timeout=timedelta(seconds=timeout_seconds),
                    )
                except asyncio.TimeoutError:
                    # Wall-clock evaluation belongs to an Activity.  The
                    # Workflow only records the deterministic timer and asks
                    # the Activity to atomically re-check/persist expiry.
                    expiry = await workflow.execute_activity(
                        "expire_logical_effects_approval_activity",
                        {"job_id": job_id},
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                    if bool((expiry or {}).get("expired")):
                        await self._cleanup(job_id)
                        return {"status": "failed", "job_id": job_id, "error": "approval_timeout"}
                continue
            if bool((result or {}).get("waiting_expansion")):
                await workflow.wait_condition(
                    lambda: self._patch_pending or self._cancel_requested or self._paused
                )
                if self._cancel_requested or self._paused:
                    continue
                self._patch_pending = False
                continue
            if bool((result or {}).get("terminal")):
                await self._cleanup(job_id)
                return {"status": str((result or {}).get("status") or "completed"), "job_id": job_id}
            if bool((result or {}).get("paused")):
                self._paused = True
                continue
            completed_frontiers += 1
            if completed_frontiers >= continue_after:
                workflow.continue_as_new(
                    {
                        "job_id": job_id,
                        "heartbeat_seconds": heartbeat_seconds,
                        "frontier_timeout_seconds": frontier_timeout,
                        "continue_after_frontiers": continue_after,
                        "completed_frontiers": 0,
                    }
                )

    def _consume_approval(self, consumed: object) -> None:
        """Remove exactly one Activity-acknowledged approval Signal.

        Signals are retained while the Workflow is waiting so an Activity
        retry can observe the same durable decision.  Once the Activity has
        applied it to the matching current node, retaining it would let old
        approvals grow in History and could incorrectly match a future node
        after a malformed plan reused an id.
        """
        if not isinstance(consumed, dict):
            return
        node_id = str(consumed.get("node_id") or "")
        approved = bool(consumed.get("approved", True))
        if not node_id:
            return
        for index, item in enumerate(self._approvals):
            if (
                str((item or {}).get("node_id") or "") == node_id
                and bool((item or {}).get("approved", True)) == approved
            ):
                del self._approvals[index]
                return

    async def _cleanup(self, job_id: str) -> None:
        try:
            await workflow.execute_activity(
                "cleanup_job_secrets_activity", job_id, start_to_close_timeout=timedelta(seconds=30)
            )
        except Exception:
            pass

    async def _fail(self, job_id: str, error: str) -> None:
        try:
            await workflow.execute_activity(
                "fail_logical_read_job_activity",
                {"job_id": job_id, "error": error[:500]},
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception:
            pass

    async def _cancel(self, job_id: str, keep_completed: bool) -> None:
        try:
            await workflow.execute_activity(
                "cancel_logical_effects_job_activity",
                {"job_id": job_id, "keep_completed": keep_completed},
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception:
            # Cancellation must still complete at the Workflow level if Redis
            # is transiently unavailable; the API control path and TTL-backed
            # reconciliation can retry the same idempotent state update.
            pass

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False
        self._wake_generation += 1

    @workflow.signal
    async def cancel_request(self, keep_completed: bool = True) -> None:
        self._cancel_requested = True
        self._keep_completed = bool(keep_completed)

    @workflow.signal
    async def approve_task(self, payload) -> None:
        if isinstance(payload, dict) and str(payload.get("node_id") or ""):
            self._approvals.append(
                {"node_id": str(payload["node_id"]), "approved": bool(payload.get("approved", True))}
            )
            self._wake_generation += 1

    @workflow.signal
    async def plan_patch_available(self) -> None:
        self._patch_pending = True
