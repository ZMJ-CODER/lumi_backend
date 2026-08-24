"""Idempotency and admission guard for job submission."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from app.agents.orchestration.admission import AdmissionBackpressureError, job_admission
from app.agents.orchestration.models import Job, JobStatus
from app.agents.orchestration.state import StateStore
from app.core.config import settings


class ActiveConversationJobError(RuntimeError):
    """同一会话已有尚未结束的办公任务。"""


class UserJobLimitError(RuntimeError):
    """单个用户同时运行的办公任务达到上限。"""


class AgentBackpressureError(RuntimeError):
    """全局办公容量或规划准入槽位已满。"""


class SubmissionGuard:
    """Serialize submission admission without knowing how a Job is executed."""

    _ACTIVE_STATUSES = frozenset({
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.PAUSED,
        JobStatus.WAITING_APPROVAL,
        JobStatus.WAITING_RESOURCES,
    })

    def __init__(self, *, store: StateStore) -> None:
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}

    async def _find_idempotent(self, user_id: str, submission_key: str) -> Job | None:
        try:
            for job_id in await self._store.list_job_ids(user_id, 5):
                job = await self._store.get_job(job_id)
                if (
                    job
                    and job.submission_key == submission_key
                    and time.time() - (job.created_at or 0) < 30
                    and job.status in self._ACTIVE_STATUSES
                ):
                    return job
        except Exception:
            return None
        return None

    async def submit(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        submission_key: str,
        create_job: Callable[[str], Awaitable[Job]],
    ) -> Job:
        existing = await self._find_idempotent(user_id, submission_key)
        if existing is not None:
            return existing
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            existing = await self._find_idempotent(user_id, submission_key)
            if existing is not None:
                return existing
            active_jobs = [
                job
                for job in await self._store.list_jobs(user_id, 50)
                if job.status in self._ACTIVE_STATUSES
            ]
            if conversation_id and any(job.conversation_id == conversation_id for job in active_jobs):
                raise ActiveConversationJobError(
                    "当前会话已有办公任务正在执行，请等待完成或主动终止任务。"
                )
            if len(active_jobs) >= settings.AGENT_USER_ACTIVE_JOB_LIMIT:
                raise UserJobLimitError("当前有任务正在进行中，请切换到普通模式对话")
            token = str(uuid.uuid4())
            try:
                await job_admission.reserve(token)
                return await create_job(token)
            except AdmissionBackpressureError as exc:
                await job_admission.release(token=token)
                # The admission kernel can reject during ``promote`` as well
                # as at the preflight store check. Preserve the public,
                # user-specific error contract in both paths.
                if "当前有任务正在进行中" in str(exc):
                    raise UserJobLimitError(str(exc)) from exc
                raise AgentBackpressureError(str(exc)) from exc
            except Exception:
                await job_admission.release(token=token)
                raise
