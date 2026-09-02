"""面向显式授权办公任务清单的滚动 Temporal 工作流。

工作流状态刻意保持精简：Job 和节点输出留在 Redis，用户凭据留在短生命周期
凭据桥中。这样既使 Temporal 历史适合长清单，也避免文档或工具负载成为工作流输入。
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError


@workflow.defn
class ManifestWorkflow:
    """Advance one persisted manifest batch per activity invocation."""

    def __init__(self) -> None:
        self._paused = False
        self._cancel_requested = False

    @workflow.run
    async def run(self, payload: dict) -> dict:
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return {"status": "failed", "error": "missing job_id"}
        heartbeat_seconds = max(5, int(payload.get("heartbeat_seconds") or 15))
        batch_timeout_seconds = max(60, int(payload.get("batch_timeout_seconds") or 1800))
        continue_after_batches = max(1, int(payload.get("continue_after_batches") or 40))
        batch_count = max(0, int(payload.get("batch_count") or 0))

        while True:
            if self._cancel_requested:
                await self._finish(job_id)
                return {"status": "cancelled", "job_id": job_id}
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                continue

            activity_task = asyncio.create_task(
                workflow.execute_activity(
                    "run_manifest_batch_activity",
                    {"job_id": job_id},
                    start_to_close_timeout=timedelta(seconds=batch_timeout_seconds),
                    heartbeat_timeout=timedelta(seconds=max(heartbeat_seconds * 2, heartbeat_seconds + 5)),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            )
            await workflow.wait_condition(
                lambda task=activity_task: task.done() or self._cancel_requested or self._paused
            )
            if self._cancel_requested:
                activity_task.cancel()
                await asyncio.gather(activity_task, return_exceptions=True)
                await self._finish(job_id)
                return {"status": "cancelled", "job_id": job_id}
            # A pause waits for the currently running batch to settle.  The
            # next batch remains unscheduled until a resume signal arrives.
            if self._paused and not activity_task.done():
                await activity_task

            try:
                result = await activity_task
            except CancelledError:
                await self._finish(job_id)
                return {"status": "cancelled", "job_id": job_id}
            except Exception as exc:  # Activity failure is reflected in Redis.
                await self._fail(job_id, str(exc))
                await self._finish(job_id)
                return {"status": "failed", "job_id": job_id, "error": str(exc)[:300]}

            if bool((result or {}).get("terminal")):
                await self._finish(job_id)
                return {"status": str((result or {}).get("status") or "completed"), "job_id": job_id}

            batch_count += 1
            if batch_count >= continue_after_batches:
                workflow.continue_as_new(
                    {
                        "job_id": job_id,
                        "heartbeat_seconds": heartbeat_seconds,
                        "batch_timeout_seconds": batch_timeout_seconds,
                        "continue_after_batches": continue_after_batches,
                        "batch_count": 0,
                    }
                )

    async def _finish(self, job_id: str) -> None:
        try:
            await workflow.execute_activity(
                "cleanup_job_secrets_activity",
                job_id,
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception:
            # Credential TTL remains the final safety backstop.
            pass

    async def _fail(self, job_id: str, error: str) -> None:
        try:
            await workflow.execute_activity(
                "fail_manifest_job_activity",
                {"job_id": job_id, "error": error[:500]},
                start_to_close_timeout=timedelta(seconds=30),
            )
        except Exception:
            # The same Redis outage may have caused the batch failure. A
            # workflow failure remains observable for operational recovery.
            pass

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False

    @workflow.signal
    async def cancel_request(self, _keep_completed: bool = True) -> None:
        self._cancel_requested = True

    @workflow.query
    def get_runtime(self) -> dict:
        return {
            "paused": self._paused,
            "cancel_requested": self._cancel_requested,
        }
