"""Replacement-subgraph service for failed office jobs.

The orchestrator owns the policy decision to replan.  Once that decision has
been made, this service creates a planner prompt from durable execution facts,
mounts the replacement graph after completed work, and persists its audit.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.plan_compilation_service import PlanCompilationService
from app.agents.orchestration.plan_context import PlanRequestContext
from app.agents.orchestration.tca import ComplexityLevel
from app.repositories.job_repository import JobRepository


class FailedJobReplanService:
    """Plan and mount one bounded replacement graph for a failed job."""

    def __init__(
        self,
        *,
        store: JobRepository,
        workers: dict,
        plan_for_level: Callable[..., Awaitable[Any]],
        plan_compilation: PlanCompilationService,
    ) -> None:
        self._store = store
        self._workers = workers
        self._plan_for_level = plan_for_level
        self._plan_compilation = plan_compilation

    @staticmethod
    def _public_text(value: object, limit: int = 800) -> str:
        text = str(value or "").strip()
        try:
            from app.core.agent_security import redact_server_text

            text = redact_server_text(text)
        except Exception:  # noqa: BLE001
            pass
        return text[:limit]

    def _execution_feedback(self, job: Job) -> tuple[list[dict], str]:
        completed_evidence: list[dict] = []
        failed_evidence: list[dict] = []
        for node in job.nodes:
            result = node.result or {}
            if node.status == TaskStatus.COMPLETED:
                completed_evidence.append(
                    {
                        "step": self._public_text(node.name or node.agent, 120),
                        "result": self._public_text(
                            result.get("content") or result.get("output"), 1200
                        ),
                        "outputs": [
                            self._public_text(
                                item.get("name") if isinstance(item, dict) else item,
                                120,
                            )
                            for item in (result.get("outputs") or [])[:5]
                        ],
                    }
                )
            elif node.status in {TaskStatus.FAILED, TaskStatus.ESCALATED}:
                failed_evidence.append(
                    {
                        "step": self._public_text(node.name or node.agent, 120),
                        "method": self._public_text(
                            result.get("tool")
                            or node.params.get("preferred_tool")
                            or node.agent,
                            120,
                        ),
                        "error_code": self._public_text(node.error_code, 80),
                        "error": self._public_text(node.error, 500),
                    }
                )
        feedback = {
            "instruction": (
                "这是同一任务的计划演进。保留已完成产物，只规划尚未完成的目标；"
                "不要重复已成功步骤；失败方法不得原样重试，应更换工具、参数或实现原理。"
            ),
            "completed": completed_evidence,
            "failed": failed_evidence,
        }
        return failed_evidence, json.dumps(feedback, ensure_ascii=False, default=str)

    async def replan(
        self,
        job: Job,
        *,
        target: ComplexityLevel,
        current: ComplexityLevel,
        upgrade_count: int,
        replan_count: int,
        outcome_category: str,
        context: dict,
        llm_api_key: str | None,
    ) -> bool:
        """Create and commit a replacement subgraph after policy approval."""
        failed_evidence, execution_feedback = self._execution_feedback(job)
        evolution_context = (context.get("prior_summaries") or "").strip()
        evolution_context += "\n\n[当前任务执行反馈]\n" + execution_feedback
        tree = await self._plan_for_level(
            target,
            PlanRequestContext.from_mapping(context)
            .with_llm_api_key(llm_api_key)
            .with_prior_summaries(evolution_context),
            bypass_fast_paths=True,
        )
        if tree.error or not tree.nodes:
            job.routing["replan_error"] = (
                tree.error or tree.clarification or "未生成可执行步骤"
            )
            await self._store.save_job(job)
            return False

        previous = {
            "level": current.value,
            "category": outcome_category,
            "steps": [
                {
                    "name": node.name,
                    "status": node.status.value,
                    "error_code": node.error_code,
                }
                for node in job.nodes
            ],
        }
        self._plan_compilation.normalize_for_replan(tree.nodes, job.request)
        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.presentation import attach_display_plan
        from app.agents.orchestration.safety import prepare_node_safety

        current_revision = int(job.routing.get("plan_revision") or 1)
        next_revision = current_revision + 1
        completed_ids = [
            node.id for node in job.nodes if node.status == TaskStatus.COMPLETED
        ]
        original_ids = {node.id for node in job.nodes}
        for node in tree.nodes:
            if node.id in original_ids:
                node.id = f"replan-{next_revision}-{uuid.uuid4().hex[:8]}"
            if not node.depends_on:
                node.depends_on = list(completed_ids)
            node.metadata = {**(node.metadata or {}), "plan_revision": next_revision}
            attach_display_plan(node)
            prepare_node_safety(node, job.user_id, job.job_id)

        anchor_nodes = [
            node for node in job.nodes if node.status == TaskStatus.COMPLETED
        ]
        errors = validate_planned_dag([*anchor_nodes, *tree.nodes], self._workers)
        if errors:
            job.routing["replan_error"] = "；".join(errors)[:500]
            await self._store.save_job(job)
            return False

        upgrades = list(job.routing.get("upgrades") or [])
        upgrades.append(
            {"from": current.value, "to": target.value, "reason": outcome_category}
        )
        attempts = list(job.routing.get("attempts") or [])
        attempts.append(previous)
        failed_names = [item["step"] for item in failed_evidence if item.get("step")]
        public_reason = (
            f"原计划中的“{'、'.join(failed_names[:2])}”未能完成，已根据执行结果更换方法。"
            if failed_names
            else "原计划未通过结果验证，已根据执行结果更换方法。"
        )
        plan_history = list(job.routing.get("plan_history") or [])
        plan_history.append(
            {
                "revision": current_revision,
                "plan_text": self._public_text(job.plan_text, 1000),
                "reason": public_reason,
                "changed_at": time.time(),
            }
        )
        retired: list[str] = []
        preserved_nodes = []
        for old_node in job.nodes:
            if old_node.status == TaskStatus.COMPLETED:
                old_node.metadata = {
                    **(old_node.metadata or {}),
                    "plan_revision": next_revision,
                }
                preserved_nodes.append(old_node)
                continue
            if old_node.status not in {
                TaskStatus.CANCELLED,
                TaskStatus.INTERRUPTED,
                TaskStatus.SKIPPED,
            }:
                old_node.status = TaskStatus.SKIPPED
                old_node.error = "已由编排器生成的替代子图接管"
                old_node.completed_at = time.time()
            retired.append(old_node.id)
        mounted = list(job.routing.get("mounted_subgraphs") or [])
        mounted.append(
            {
                "revision": next_revision,
                "anchor_nodes": completed_ids,
                "retired_nodes": retired,
                "node_ids": [node.id for node in tree.nodes],
                "reason": outcome_category,
            }
        )
        job.routing.update(
            {
                "level": target.value,
                "mode": "react" if target == ComplexityLevel.M3 else "plan_execute",
                "upgrade_count": upgrade_count + int(target != current),
                "replan_count": replan_count + 1,
                "upgrades": upgrades,
                "attempts": attempts[-2:],
                "plan_revision": next_revision,
                "plan_history": plan_history[-3:],
                "plan_change_reason": public_reason,
                "mounted_subgraphs": mounted[-6:],
            }
        )
        job.nodes = [*preserved_nodes, *tree.nodes]
        job.plan_text = tree.plan_text
        job.status = JobStatus.RUNNING
        job.error = None
        job.result = None
        job.updated_at = time.time()
        await self._store.save_job(job)
        try:
            from app.core.observability import inc_agent_replan

            inc_agent_replan(current.value, target.value, outcome_category)
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "办公任务失败升级: job={} {}->{} reason={}",
            job.job_id[:8],
            current.value,
            target.value,
            outcome_category,
        )
        return True
