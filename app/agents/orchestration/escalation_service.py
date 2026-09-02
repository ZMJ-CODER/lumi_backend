"""任务级升级信号的确定性处理。"""

from __future__ import annotations

import time

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.state import StateStore
from app.core.config import settings


class EscalationService:
    """Resolve L2 signals without allowing workers to mutate graph topology."""

    def __init__(self, *, store: StateStore) -> None:
        self._store = store

    async def handle_task_escalation(self, job: Job) -> bool:
        """Apply an approval/clarification signal and persist the new state."""
        from app.agents.orchestration.escalation import (
            EscalationLevel,
            EscalationReason,
            coerce_escalation,
        )

        escalated = next(
            (node for node in job.nodes if node.status == TaskStatus.ESCALATED),
            None,
        )
        if escalated is None:
            return False
        signal = coerce_escalation(
            (escalated.metadata or {}).get("escalation"),
            default_node_id=escalated.id,
        )
        if signal is None or signal.level != EscalationLevel.TASK:
            return False

        job.routing = dict(job.routing or {})
        events = list(job.routing.get("escalations") or [])
        events.append(
            {
                "level": signal.level.value,
                "reason": signal.reason.value,
                "node_id": escalated.id,
                "at": time.time(),
            }
        )
        job.routing["escalations"] = events[-12:]

        if signal.reason == EscalationReason.APPROVAL_REQUIRED:
            tool = str(
                (escalated.result or {}).get("tool")
                or (escalated.params or {}).get("preferred_tool")
                or ""
            )
            tool_metadata = (escalated.result or {}).get("tool_metadata")
            if not isinstance(tool_metadata, dict):
                tool_metadata = {}
            approval_fingerprint = str(
                (escalated.result or {}).get("approval_fingerprint")
                or tool_metadata.get("approval_fingerprint")
                or ""
            )
            if not tool or not approval_fingerprint:
                job.status = JobStatus.FAILED
                job.error = "高风险步骤未提供完整的工具与参数审批标识，已停止执行。"
            else:
                metadata = dict(escalated.metadata or {})
                metadata["awaiting_approval"] = True
                metadata["approval_tool"] = tool
                metadata["approval_fingerprint"] = approval_fingerprint
                metadata["approval_expires_at"] = time.time() + max(
                    60, int(settings.AGENT_APPROVAL_TIMEOUT_SECONDS)
                )
                metadata["escalation"] = signal.model_dump(mode="json")
                escalated.metadata = metadata
                escalated.status = TaskStatus.PENDING
                escalated.error = None
                escalated.error_code = None
                job.status = JobStatus.WAITING_APPROVAL
                job.error = signal.message or "该步骤需要你的确认后才能继续。"
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True

        if signal.reason in {
            EscalationReason.MISSING_PREREQUISITE,
            EscalationReason.PRECONDITION_FALSE,
        } or signal.requires_user_input:
            job.status = JobStatus.COMPLETED
            job.error = None
            job.result = {
                "type": "clarification",
                "question": signal.message or "完成该任务还需要补充必要信息。",
                "escalation": signal.model_dump(mode="json"),
            }
            job.updated_at = time.time()
            await self._store.save_job(job)
            return True
        return False
