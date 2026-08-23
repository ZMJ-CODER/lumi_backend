"""Shared terminal-job side effects for every execution runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.agents.orchestration.admission import job_admission
from app.agents.orchestration.models import Job
from app.agents.orchestration.state_machine.policies import TERMINAL_JOB_STATUSES


def is_terminal_job(job: Job | None) -> bool:
    return bool(job and job.status in TERMINAL_JOB_STATUSES)


class JobFinalizer:
    """Run terminal-only cleanup once a runtime has persisted its final Job."""

    def __init__(
        self,
        *,
        stop_heartbeat: Callable[[str], Awaitable[None]],
        on_summary: Callable[[Job], Awaitable[None]],
        on_task_index: Callable[[Job], Awaitable[None]],
        on_metric: Callable[[Job], Awaitable[None]],
        on_learning: Callable[[Job], Awaitable[None]],
        on_terminal: Callable[[Job], None] | None = None,
        release_capacity: Callable[[Job], Awaitable[None]] | None = None,
    ) -> None:
        self._stop_heartbeat = stop_heartbeat
        self._on_summary = on_summary
        self._on_task_index = on_task_index
        self._on_metric = on_metric
        self._on_learning = on_learning
        self._on_terminal = on_terminal or (lambda _job: None)
        self._release_capacity = release_capacity or self._default_release_capacity

    @staticmethod
    async def _default_release_capacity(job: Job) -> None:
        await job_admission.release(job_id=job.job_id, user_id=job.user_id)

    async def finalize(self, job: Job | None) -> bool:
        """Apply terminal cleanup and return whether this was a terminal Job."""
        if not is_terminal_job(job):
            return False
        assert job is not None
        await self._on_summary(job)
        await self._on_task_index(job)
        await self._on_metric(job)
        await self._on_learning(job)
        await self._release_capacity(job)
        await self._stop_heartbeat(job.job_id)
        self._on_terminal(job)
        return True

    async def suspend_capacity(self, job: Job | None) -> bool:
        """Release admission/heartbeat for a non-terminal suspended job.

        Waiting approval/resource jobs remain queryable and resumable, but do
        not count against global or per-user active-task quotas.
        """
        if job is None:
            return False
        await self._release_capacity(job)
        await self._stop_heartbeat(job.job_id)
        return True
