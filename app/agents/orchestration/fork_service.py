"""Create safe replay branches from completed orchestration jobs."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from app.agents.orchestration.models import Job, JobStatus, TaskStatus
from app.agents.orchestration.runtime_gateway import RuntimeGateway
from app.agents.orchestration.submission_guard import UserJobLimitError
from app.agents.orchestration.admission import job_admission
from app.core.config import settings
from app.repositories.job_repository import JobRepository


class JobForkService:
    """Own the forward-only replay safety checks and branch submission."""

    def __init__(
        self,
        *,
        repository: JobRepository,
        workers: dict | None = None,
        list_jobs: Callable[[str, int], Awaitable[list[Job]]],
        start_heartbeat: Callable[[str, str], None],
        live_jobs: dict[str, Job],
        tasks: dict[str, object],
        api_keys: dict[str, str],
        llm_configs: dict[str, dict],
        plan_context: dict[str, dict],
        run_job: Callable[[str], Awaitable[None]],
    ) -> None:
        self._repository = repository
        self._workers = workers if workers is not None else WORKERS
        self._list_jobs = list_jobs
        self._start_heartbeat = start_heartbeat
        self._live_jobs = live_jobs
        self._tasks = tasks
        self._api_keys = api_keys
        self._llm_configs = llm_configs
        self._plan_context = plan_context
        self._run_job = run_job

    @staticmethod
    def _ancestor_ids(nodes, node_id: str) -> set[str]:
        by_id = {node.id: node for node in nodes}
        seen: set[str] = set()
        target = by_id.get(node_id)
        pending = list(target.depends_on if target else [])
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            node = by_id.get(current)
            if node is None:
                continue
            seen.add(current)
            pending.extend(node.depends_on)
        return seen

    @staticmethod
    def _descendant_ids(nodes, node_id: str) -> set[str]:
        children: dict[str, list[str]] = {node.id: [] for node in nodes}
        for node in nodes:
            for dependency in node.depends_on:
                if dependency in children:
                    children[dependency].append(node.id)
        seen = {node_id}
        pending = [node_id]
        while pending:
            current = pending.pop()
            for child in children.get(current, []):
                if child not in seen:
                    seen.add(child)
                    pending.append(child)
        return seen

    async def fork(
        self,
        job_id: str,
        *,
        node_id: str,
        params: dict | None = None,
        instruction: str | None = None,
        llm_api_key: str | None = None,
    ) -> Job:
        source = await self._repository.get_job(job_id)
        if source is None:
            raise RuntimeError("源任务不存在")
        if source.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }:
            raise RuntimeError("只能从已结束的任务创建分支")
        if RuntimeGateway.is_manifest_job(source) or isinstance((source.routing or {}).get("manifest"), dict):
            raise RuntimeError("滚动清单的历史节点仍在压缩归档，暂不支持从单个节点回放")

        by_id = {node.id: node for node in source.nodes}
        target = by_id.get(node_id)
        if target is None:
            raise RuntimeError("回放节点不存在")
        prefix = self._ancestor_ids(source.nodes, node_id)
        rerun_ids = self._descendant_ids(source.nodes, node_id)
        from app.agents.orchestration.safety import is_effectful, prepare_node_safety
        from app.agents.orchestration.execution_lineage import ensure_node_result_ref, resolve_result_ref

        retained_ids = {node.id for node in source.nodes if node.id not in rerun_ids}
        committed_prefix_effects = [
            by_id[ancestor_id]
            for ancestor_id in prefix
            if by_id[ancestor_id].effect_status == "committed"
        ]
        if committed_prefix_effects:
            names = ", ".join(node.name or node.id for node in committed_prefix_effects[:3])
            raise RuntimeError(f"回放点上游包含已提交的副作用步骤（{names}），第一版不支持跨该边界分支")
        for retained_id in retained_ids:
            upstream = by_id[retained_id]
            if upstream.status != TaskStatus.COMPLETED:
                raise RuntimeError("分支外存在未完成步骤，不能安全复用执行前缀")
            result_ref = await ensure_node_result_ref(source.user_id, upstream)
            if not result_ref or not await resolve_result_ref(source.user_id, result_ref):
                raise RuntimeError("已完成步骤的结果引用已过期或不可验证，不能安全创建分支")
        if is_effectful(target) and target.effect_status == "committed":
            raise RuntimeError("选中的节点已产生副作用，第一版不支持从该节点重新分支")
        if retained_ids:
            await self._repository.save_job(source)

        admission_token = str(uuid.uuid4())
        try:
            await job_admission.reserve(admission_token)
            active_statuses = {
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.PAUSED,
                JobStatus.WAITING_APPROVAL,
            }
            active_jobs = [
                job for job in await self._list_jobs(source.user_id, 50)
                if job.status in active_statuses
            ]
            if len(active_jobs) >= settings.AGENT_USER_ACTIVE_JOB_LIMIT:
                raise UserJobLimitError("当前有任务正在进行中，请等待其完成后再创建分支")

            new_job_id = str(uuid.uuid4())
            forked_nodes = [node.model_copy(deep=True) for node in source.nodes]
            for node in forked_nodes:
                node.metadata = dict(node.metadata or {})
                if node.id in retained_ids:
                    node.status = TaskStatus.COMPLETED
                    node.result = None
                    node.error = None
                    node.error_code = None
                    node.started_at = None
                    node.completed_at = None
                    node.retries = 0
                    node.metadata["replay_prefix"] = True
                    continue
                node.status = TaskStatus.PENDING
                node.result = None
                node.error = None
                node.error_code = None
                node.retries = 0
                node.started_at = None
                node.completed_at = None
                node.effect_status = None
                node.idempotency_key = None
                for key in (
                    "result_ref", "dependency_results", "awaiting_approval",
                    "approval_fingerprint", "confirmed_tools", "confirmed_tool_calls",
                ):
                    node.metadata.pop(key, None)
            forked_target = next(node for node in forked_nodes if node.id == node_id)
            if params:
                forked_target.params = {**forked_target.params, **dict(params)}
            if instruction is not None:
                forked_target.params = {**forked_target.params, "instruction": str(instruction)[:4000]}

            routing = dict(source.routing or {})
            routing.update({
                "runtime": "legacy",
                "execution_kind": "fork",
                "fork": {
                    "parent_execution_id": source.execution_id or source.job_id,
                    "parent_job_id": source.job_id,
                    "forked_from_node_id": node_id,
                    "prefix_node_ids": sorted(prefix),
                    "created_at": time.time(),
                },
            })
            job = Job(
                job_id=new_job_id,
                execution_id=new_job_id,
                parent_execution_id=source.execution_id or source.job_id,
                root_execution_id=source.root_execution_id or source.execution_id or source.job_id,
                forked_from_node_id=node_id,
                user_id=source.user_id,
                user_role=source.user_role,
                request=source.request,
                scene=source.scene,
                conversation_id=source.conversation_id,
                status=JobStatus.RUNNING,
                nodes=forked_nodes,
                plan_text=source.plan_text,
                routing=routing,
            )
            for node in job.nodes:
                prepare_node_safety(node, job.user_id, job.job_id)
            from app.agents.orchestration.dag import validate_planned_dag

            errors = validate_planned_dag(job.nodes, self._workers)
            if errors:
                raise RuntimeError("分支计划校验失败：" + "；".join(errors[:3]))
            await job_admission.promote(admission_token, job.job_id, job.user_id)
            self._start_heartbeat(job.job_id, job.user_id)
            await self._repository.create_job(job)
            self._live_jobs[job.job_id] = job
            source_config = self._llm_configs.get(source.job_id) or (
                self._plan_context.get(source.job_id) or {}
            ).get("llm_config")
            if source_config:
                self._llm_configs[job.job_id] = dict(source_config)
                self._plan_context[job.job_id] = {"llm_config": dict(source_config)}
                self._api_keys[job.job_id] = str(source_config.get("api_key") or "")
            elif llm_api_key:
                self._api_keys[job.job_id] = llm_api_key
            self._tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
            return job
        except UserJobLimitError:
            await job_admission.release(token=admission_token)
            raise
        except Exception:
            await job_admission.release(token=admission_token)
            raise
