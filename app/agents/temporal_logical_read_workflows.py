"""已持久化纯读逻辑计划的 Temporal Workflow。

完整计划和节点输出都在 Workflow History 之外。该 Workflow 不访问模型、Redis
或系统时间；每轮委托一个 Activity 执行前沿，仅保留 Job 引用和计数器。
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import CancelledError


@workflow.defn
class LogicalReadWorkflow:
    """每次推进一个不可变 Redis 逻辑计划的纯读前沿。"""

    def __init__(self) -> None:
        self._paused = False
        self._cancel_requested = False
        self._patch_pending = False

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
                await self._cleanup(job_id)
                return {"status": "cancelled", "job_id": job_id}
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                continue
            task = asyncio.create_task(
                workflow.execute_activity(
                    "run_logical_read_frontier_activity",
                    {"job_id": job_id},
                    start_to_close_timeout=timedelta(seconds=frontier_timeout),
                    heartbeat_timeout=timedelta(seconds=max(heartbeat_seconds * 2, heartbeat_seconds + 5)),
                    # 纯读 Activity 可重试：下一前沿选择前会持久化已完成节点，
                    # 因此重试不会重复已提交的逻辑节点。
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            )
            await workflow.wait_condition(lambda task=task: task.done() or self._paused or self._cancel_requested)
            if self._cancel_requested:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
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
            if bool((result or {}).get("terminal")):
                await self._cleanup(job_id)
                return {"status": str((result or {}).get("status") or "completed"), "job_id": job_id}
            if bool((result or {}).get("replan_required")):
                try:
                    replan = await workflow.execute_activity(
                        "replan_logical_read_activity",
                        {"job_id": job_id},
                        start_to_close_timeout=timedelta(seconds=180),
                        # A replan invokes an LLM. Repeating it after an
                        # uncertain Activity failure could mount a different
                        # tail, so it has exactly one Temporal attempt.
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                except Exception as exc:
                    await self._fail(job_id, str(exc))
                    await self._cleanup(job_id)
                    return {"status": "failed", "job_id": job_id, "error": str(exc)[:300]}
                if not bool((replan or {}).get("allowed")):
                    await self._cleanup(job_id)
                    return {
                        "status": "failed",
                        "job_id": job_id,
                        "error": str((replan or {}).get("reason") or "replan_blocked")[:300],
                    }
                # A successful replan materializes and persists a new frontier.
                # Do not count it as a completed frontier; the next loop owns
                # the first execution of that replacement.
                continue
            if bool((result or {}).get("waiting_expansion")):
                # 补丁正文只保存在 Redis 逻辑计划；Signal 仅是可重复的唤醒
                # 事件。保留 bool 可避免 Signal 比 Activity 返回更早时丢失。
                await workflow.wait_condition(
                    lambda: self._patch_pending or self._cancel_requested or self._paused
                )
                if self._cancel_requested:
                    continue
                if self._paused:
                    continue
                self._patch_pending = False
                continue
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

    @workflow.signal
    async def pause(self) -> None:
        self._paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._paused = False

    @workflow.signal
    async def cancel_request(self, _keep_completed: bool = True) -> None:
        self._cancel_requested = True

    @workflow.signal
    async def plan_patch_available(self) -> None:
        self._patch_pending = True
