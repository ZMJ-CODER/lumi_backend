"""Approval-gate state updates for orchestration control plane."""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.state import StateStore
from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    job: Job
    approved: bool
    node_id: str


class ApprovalService:
    """Validate and persist an orchestrator-owned approval decision."""

    def __init__(self, *, store: StateStore) -> None:
        self._store = store

    async def expire_if_due(self, job: Job, *, now: float | None = None) -> bool:
        """Fail an expired approval gate; safe to call from polling paths."""
        if job.status != JobStatus.WAITING_APPROVAL:
            return False
        now = time.time() if now is None else now
        waiting = next((node for node in job.nodes if (node.metadata or {}).get("awaiting_approval")), None)
        expires_at = float((waiting.metadata or {}).get("approval_expires_at") or 0) if waiting else 0
        if not waiting or not expires_at or now < expires_at:
            return False
        waiting.status = TaskStatus.SKIPPED
        waiting.error = "审批等待已超时"
        waiting.error_code = "APPROVAL_TIMEOUT"
        waiting.completed_at = now
        job.status = JobStatus.FAILED
        job.error = "高风险步骤在审批时限内未获确认，任务已自动停止。"
        job.updated_at = now
        await self._store.save_job(job)
        return True

    async def resolve(self, job_id: str, node_id: str, approved: bool) -> ApprovalResult:
        job = await self._store.get_job(job_id)
        if job is None:
            raise RuntimeError("任务不存在")
        node = next((item for item in job.nodes if item.id == node_id), None)
        if node is None or not (node.metadata or {}).get("awaiting_approval"):
            raise RuntimeError("该任务节点当前不在等待审批")
        if await self.expire_if_due(job):
            raise RuntimeError("审批等待已超时，任务已停止")
        if not approved:
            node.status = TaskStatus.SKIPPED
            node.error = "用户拒绝审批"
            node.completed_at = time.time()
            job.status = JobStatus.FAILED
            job.error = "用户拒绝了高风险步骤的执行"
        else:
            metadata = dict(node.metadata or {})
            tool = str(metadata.get("approval_tool") or "")
            fingerprint = str(metadata.get("approval_fingerprint") or "")
            if not tool or not fingerprint:
                raise RuntimeError("审批凭证缺少工具参数绑定，不能安全恢复该步骤")
            metadata.pop("awaiting_approval", None)
            metadata["confirmed_tools"] = sorted({
                *(str(value) for value in (metadata.get("confirmed_tools") or [])), tool,
            } - {""})
            metadata["confirmed_tool_calls"] = sorted({
                *(str(value) for value in (metadata.get("confirmed_tool_calls") or [])), fingerprint,
            } - {""})
            node.metadata = metadata
            node.status = TaskStatus.PENDING
            node.error = None
            node.error_code = None
            job.status = JobStatus.RUNNING
            job.error = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        return ApprovalResult(job=job, approved=approved, node_id=node_id)
