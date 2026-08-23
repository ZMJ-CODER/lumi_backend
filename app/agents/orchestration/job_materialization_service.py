"""Turn a compiled plan into a validated execution-window Job."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.agents.orchestration.models import Job, JobStatus
from app.core.config import settings


@dataclass(slots=True)
class MaterializedJob:
    job: Job
    terminal: bool


class JobMaterializationService:
    """Own Job construction, static validation, and logical-plan windowing."""

    def __init__(self, *, workers: dict) -> None:
        self._workers = workers

    async def materialize(
        self,
        *,
        user_id: str,
        user_role: str,
        request: str,
        scene: str,
        conversation_id: str | None,
        submission_key: str,
        tree: Any,
        routing: dict,
    ) -> MaterializedJob:
        from app.agents.orchestration.presentation import attach_display_plan

        plan_revision = int(routing.get("plan_revision") or 1) if scene == "office" else 1
        for node in tree.nodes:
            node.metadata = {**(node.metadata or {}), "plan_revision": plan_revision}
            attach_display_plan(node)
        job = Job(
            job_id=str(uuid.uuid4()),
            user_id=user_id,
            user_role=user_role,
            request=request,
            scene=scene,
            conversation_id=conversation_id,
            submission_key=submission_key,
            status=JobStatus.RUNNING,
            nodes=tree.nodes,
            plan_text=tree.plan_text,
            routing=routing,
        )
        job.execution_id = job.job_id
        job.root_execution_id = job.job_id
        if tree.error:
            job.status = JobStatus.FAILED
            job.error = tree.error
            job.result = {
                "type": "planning_error",
                "error_code": tree.error_code or "PLANNING_ERROR",
                "message": tree.error,
            }
            logger.warning("办公任务规划已停止: {} | {}", job.job_id[:8], tree.error)
            return MaterializedJob(job, terminal=True)

        from app.agents.orchestration.dag import validate_planned_dag
        from app.agents.orchestration.safety import prepare_node_safety

        for node in job.nodes:
            prepare_node_safety(node, user_id, job.job_id)
        dag_errors = validate_planned_dag(job.nodes, self._workers)
        if dag_errors:
            detail = "；".join(dag_errors)[:500]
            logger.warning("任务 DAG 校验失败，终止任务: {} | {}", job.job_id[:8], detail)
            job.status = JobStatus.FAILED
            job.error = "任务规划校验失败，未执行任何工具。请稍后重试；若持续出现，请切换模型或检查任务描述。"
            job.result = {
                "type": "planning_error",
                "error_code": "DAG_VALIDATION_ERROR",
                "detail": detail,
                "message": job.error,
            }
            return MaterializedJob(job, terminal=True)

        if (
            scene == "office"
            and settings.AGENT_LOGICAL_PLAN_ENABLED
            and not routing.get("manifest")
            and len(job.nodes) >= settings.AGENT_LOGICAL_PLAN_MIN_NODES
        ):
            from app.agents.orchestration.logical_plan import (
                create_logical_plan,
                logical_plan_progress,
                materialize_frontier,
                save_logical_plan,
            )

            logical_plan = await create_logical_plan(user_id, job.nodes)
            if logical_plan["budget"]["estimated_total"] > logical_plan["budget"]["limit"]:
                job.status = JobStatus.COMPLETED
                job.result = {
                    "type": "clarification",
                    "question": (
                        f"该任务预估消耗约 {logical_plan['budget']['estimated_total']} token，"
                        "超过当前单次任务预算。请拆分任务后重试，或明确确认较高预算。"
                    ),
                }
                job.nodes = []
                job.routing["logical_plan"] = {
                    "plan_id": logical_plan["plan_id"],
                    "state": "budget_confirmation",
                    "progress": logical_plan_progress(logical_plan),
                }
                return MaterializedJob(job, terminal=True)
            frontier = materialize_frontier(logical_plan)
            if not frontier:
                job.status = JobStatus.FAILED
                job.error = "任务计划没有可执行前沿，已停止以避免无效调度。"
                job.nodes = []
                job.routing["logical_plan"] = {
                    "plan_id": logical_plan["plan_id"],
                    "state": "blocked",
                    "progress": logical_plan_progress(logical_plan),
                }
                return MaterializedJob(job, terminal=True)
            await save_logical_plan(user_id, logical_plan)
            job.nodes = frontier
            job.routing["logical_plan"] = {
                "plan_id": logical_plan["plan_id"],
                "revision": logical_plan["revision"],
                "frontier_size": len(frontier),
                "progress": logical_plan_progress(logical_plan),
                "estimated_tokens": logical_plan["budget"]["estimated_total"],
            }
        if tree.clarification and not tree.nodes:
            job.status = JobStatus.COMPLETED
            job.result = {"type": "clarification", "question": tree.clarification}
            logger.info("任务需澄清（不启动执行）: {} | {}", job.job_id[:8], tree.clarification[:80])
            return MaterializedJob(job, terminal=True)
        return MaterializedJob(job, terminal=False)
