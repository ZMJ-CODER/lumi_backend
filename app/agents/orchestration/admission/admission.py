"""编排准入内核的 Lumi Redis/配置适配器。"""

from __future__ import annotations

from typing import Any

from lumi_orch.admission import (
    AdmissionBackpressureError,
    AdmissionLimits,
    JobAdmission as KernelJobAdmission,
)

from app.core.config import settings


class JobAdmission(KernelJobAdmission):
    """Binds admission protocol semantics to Lumi's infrastructure settings."""

    def __init__(self) -> None:
        # Bind the settings-backed provider explicitly; the kernel's fallback
        # limits are intentionally only for standalone package consumers.
        super().__init__(limits_provider=self.limits)

    @staticmethod
    def limits() -> AdmissionLimits:
        return AdmissionLimits(
            lease_seconds=max(60, settings.AGENT_ADMISSION_LEASE_SECONDS),
            max_inflight=settings.AGENT_SUBMISSION_MAX_INFLIGHT,
            max_active_jobs=settings.AGENT_GLOBAL_ACTIVE_JOB_LIMIT,
            max_active_jobs_per_user=settings.AGENT_USER_ACTIVE_JOB_LIMIT,
        )

    async def _redis(self) -> Any | None:
        try:
            from app.core.redis import get_redis

            return get_redis()
        except Exception:  # noqa: BLE001
            return None

    async def reserve(self, token: str) -> None:
        try:
            await super().reserve(token)
        except AdmissionBackpressureError as exc:
            if str(exc) == "admission submission capacity is full":
                raise AdmissionBackpressureError(
                    "办公任务正在繁忙处理，请稍后重试或切换普通模式对话"
                ) from None
            raise

    async def promote(self, token: str, job_id: str, user_id: str) -> None:
        try:
            await super().promote(token, job_id, user_id)
        except AdmissionBackpressureError as exc:
            messages = {
                "admission active capacity is full": "办公任务容量已满，请稍后重试或切换普通模式对话",
                "admission user active capacity is full": "当前有任务正在进行中，请切换到普通模式对话",
            }
            if message := messages.get(str(exc)):
                raise AdmissionBackpressureError(message) from None
            raise

    async def activate(self, job_id: str, user_id: str) -> None:
        try:
            await super().activate(job_id, user_id)
        except AdmissionBackpressureError as exc:
            messages = {
                "admission active capacity is full": "办公任务容量已满，请稍后恢复任务",
                "admission user active capacity is full": "当前有任务正在进行中，请稍后恢复任务",
            }
            if message := messages.get(str(exc)):
                raise AdmissionBackpressureError(message) from None
            raise


job_admission = JobAdmission()
