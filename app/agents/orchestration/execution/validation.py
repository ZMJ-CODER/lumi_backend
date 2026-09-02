"""应用执行的计划校验与 DAG 运行入口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from lumi_orch.dag import DagValidationError, validate_dag
from lumi_orch.ports import JobStateStorePort, NodeWorkerPort, ReviewPort

from app.agents.orchestration.execution.service import ApplicationTaskExecutionService
from app.agents.orchestration.models import Job, TaskNode
from app.core.config import settings


_REQUIRED_PARAMS: dict[str, list[str]] = {
    "direct_llm": ["instruction"],
    "collect_results": ["items"],
    "atomic_step": ["instruction", "preferred_tool"],
    "react_step": ["instruction"],
    "office_doc": ["doc_id", "instruction", "mode"],
    "office_text": ["instruction"],
    "office_research": ["instruction", "mode"],
    "office_todo": ["action"],
    "retrieval": ["query"],
    "document_targeting": ["query", "office_docs"],
    "web_research": ["instruction"],
    "code": ["project_id", "instruction"],
    "code_reader": ["project_id", "instruction"],
    "code_writer": ["project_id", "instruction"],
}


def validate_planned_dag(nodes: list[TaskNode], workers: dict | None = None) -> list[str]:
    """Validate agent registration, required parameters and graph structure."""
    if workers is None:
        from app.agents.orchestration.workers import WORKERS

        workers = WORKERS
    errors: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            errors.append(f"节点 id 重复: {node.id}")
        seen.add(node.id)
        if node.agent not in workers:
            errors.append(f"agent 未注册: {node.agent}")
            continue
        errors.extend(
            f"{node.agent} 缺少必选参数 {parameter}"
            for parameter in _REQUIRED_PARAMS.get(node.agent, [])
            if not node.params.get(parameter)
        )
    try:
        validate_dag(nodes)
    except DagValidationError as exc:
        errors.append(str(exc))
    return errors


async def execute_dag(
    job: Job,
    workers: Mapping[str, NodeWorkerPort],
    review: ReviewPort,
    store: JobStateStorePort,
    *,
    concurrency: int | None = None,
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    on_waiting_resources: Callable[[Job], Awaitable[None]] | None = None,
    ensure_active_capacity: Callable[[Job], Awaitable[bool]] | None = None,
) -> Job:
    """将已校验 Job 交给应用执行服务完成。"""
    validate_dag(job.nodes)
    await ApplicationTaskExecutionService(store=store, workers=workers, review=review).execute(
        job,
        concurrency=concurrency or settings.AGENT_NODE_CONCURRENCY,
        llm_api_key=llm_api_key,
        llm_config=llm_config,
        on_waiting_resources=on_waiting_resources,
        ensure_active_capacity=ensure_active_capacity,
    )
    return await store.get_job(job.job_id) or job


__all__ = ["DagValidationError", "execute_dag", "validate_planned_dag"]
