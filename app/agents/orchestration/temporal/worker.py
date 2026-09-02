"""Temporal 工作进程入口：注册 AgentDagWorkflow 与执行活动。

启动方式（项目根目录）：
    .venv\\Scripts\\python.exe -m app.agents.orchestration.temporal.worker

先启动 Temporal 开发服务器（temporal.exe server start-dev --namespace default），
再启动本 worker；编排器提交任务后由本进程执行节点 Activity。
"""

import asyncio

from loguru import logger
from temporalio.worker import Worker

from app.core.config import settings


async def main() -> None:
    from temporalio.client import Client

    from app.agents.skills.registry import init_skills
    from app.core.redis import init_redis

    await init_redis()  # 客户端技能 / BYOK 桥接 / 审计日志依赖 Redis
    init_skills()  # 加载 plugins/skills 技能插件（与 API 进程一致）
    client = await Client.connect(
        settings.TEMPORAL_ADDRESS, namespace=settings.TEMPORAL_NAMESPACE
    )
    worker = build_worker(client)
    manifest_worker = build_manifest_worker(client)
    logical_read_worker = build_logical_read_worker(client)
    logical_effects_worker = build_logical_effects_worker(client)
    logger.info(
        "Temporal Worker 已启动: {} ns={} queue={}",
        settings.TEMPORAL_ADDRESS,
        settings.TEMPORAL_NAMESPACE,
        f"{settings.TEMPORAL_TASK_QUEUE}, {settings.TEMPORAL_MANIFEST_TASK_QUEUE}, "
        f"{settings.TEMPORAL_LOGICAL_READ_TASK_QUEUE}, {settings.TEMPORAL_LOGICAL_EFFECTS_TASK_QUEUE}",
    )
    await asyncio.gather(
        worker.run(), manifest_worker.run(), logical_read_worker.run(), logical_effects_worker.run()
    )


def build_worker(client) -> "Worker":
    """构造 Temporal Worker（注册 AgentDagWorkflow + 执行 Activities）."""
    from app.agents.orchestration.temporal.activities import (
        cleanup_job_secrets_activity,
        execute_node_activity,
        persist_node_result_ref_activity,
        replan_static_job_activity,
        synthesize_final_answer_activity,
    )
    from app.agents.temporal_workflows import AgentDagWorkflow, NodeExecutionWorkflow

    return Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[AgentDagWorkflow, NodeExecutionWorkflow],
        activities=[
            execute_node_activity,
            persist_node_result_ref_activity,
            replan_static_job_activity,
            cleanup_job_secrets_activity,
            synthesize_final_answer_activity,
        ],
    )


def build_manifest_worker(client) -> "Worker":
    """Worker for the rolling manifest runtime, isolated from frozen static DAGs."""
    from app.agents.orchestration.temporal.activities import cleanup_job_secrets_activity
    from app.agents.orchestration.temporal.manifest_activities import (
        fail_manifest_job_activity,
        run_manifest_batch_activity,
    )
    from app.agents.temporal_manifest_workflows import ManifestWorkflow

    return Worker(
        client,
        task_queue=settings.TEMPORAL_MANIFEST_TASK_QUEUE,
        workflows=[ManifestWorkflow],
        activities=[
            run_manifest_batch_activity,
            fail_manifest_job_activity,
            cleanup_job_secrets_activity,
        ],
    )


def build_logical_read_worker(client) -> "Worker":
    """纯读滚动逻辑计划 Worker，与写操作/ReAct 隔离。"""
    from app.agents.orchestration.temporal.activities import cleanup_job_secrets_activity
    from app.agents.orchestration.temporal.logical_read_activities import (
        fail_logical_read_job_activity,
        replan_logical_read_activity,
        run_logical_read_frontier_activity,
    )
    from app.agents.temporal_logical_read_workflows import LogicalReadWorkflow

    return Worker(
        client,
        task_queue=settings.TEMPORAL_LOGICAL_READ_TASK_QUEUE,
        workflows=[LogicalReadWorkflow],
        activities=[
            run_logical_read_frontier_activity,
            replan_logical_read_activity,
            fail_logical_read_job_activity,
            cleanup_job_secrets_activity,
        ],
    )


def build_logical_effects_worker(client) -> "Worker":
    """预声明审批副作用逻辑计划 Worker，与纯读路径分队列隔离。"""
    from app.agents.orchestration.temporal.activities import cleanup_job_secrets_activity
    from app.agents.orchestration.temporal.logical_read_activities import (
        cancel_logical_effects_job_activity,
        expire_logical_effects_approval_activity,
        fail_logical_read_job_activity,
        run_logical_effects_frontier_activity,
    )
    from app.agents.temporal_logical_effects_workflows import LogicalEffectsWorkflow

    return Worker(
        client,
        task_queue=settings.TEMPORAL_LOGICAL_EFFECTS_TASK_QUEUE,
        workflows=[LogicalEffectsWorkflow],
        activities=[
            run_logical_effects_frontier_activity,
            expire_logical_effects_approval_activity,
            cancel_logical_effects_job_activity,
            fail_logical_read_job_activity,
            cleanup_job_secrets_activity,
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
