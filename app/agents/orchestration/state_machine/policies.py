"""编排内核策略枚举在 Lumi 中的直接导出。"""

from __future__ import annotations

from lumi_orch.policies import (
    is_terminal,
    may_escalate,
    may_replan,
    may_retry,
)

from app.agents.orchestration.models import JobStatus


TERMINAL_JOB_STATUSES = frozenset({
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.INTERRUPTED,
})

__all__ = [
    "TERMINAL_JOB_STATUSES",
    "is_terminal",
    "may_escalate",
    "may_replan",
    "may_retry",
]
