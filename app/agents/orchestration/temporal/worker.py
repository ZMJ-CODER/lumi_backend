"""Temporal Worker 进程入口 —— 注册 AgentDagWorkflow 与执行 Activities.

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
    logger.info(
        "Temporal Worker 已启动: {} ns={} queue={}",
        settings.TEMPORAL_ADDRESS,
        settings.TEMPORAL_NAMESPACE,
        settings.TEMPORAL_TASK_QUEUE,
    )
    await worker.run()


def build_worker(client) -> "Worker":
    """构造 Temporal Worker（注册 AgentDagWorkflow + 执行 Activities）."""
    from app.agents.orchestration.temporal.activities import (
        cleanup_job_secrets_activity,
        execute_node_activity,
        synthesize_final_answer_activity,
    )
    from app.agents.temporal_workflows import AgentDagWorkflow

    return Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[AgentDagWorkflow],
        activities=[
            execute_node_activity,
            cleanup_job_secrets_activity,
            synthesize_final_answer_activity,
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
