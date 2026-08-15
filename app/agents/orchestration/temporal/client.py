"""Temporal 客户端集成层 —— workflow 启动 / 查询 / 信号 + BYOK key 临时桥接.

BYOK 说明：用户自备 API key 不进入 workflow 输入（会被 Temporal 持久化到
workflow history），改为存 Redis（短 TTL），执行 Activity 时按 job_id 取用，
任务正常结束删除；取消/中断由 TTL 兜底清理。
"""

import asyncio

from loguru import logger
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from app.agents.temporal_workflows import AgentDagWorkflow
from app.core.config import settings
from app.core.redis import get_redis

_client: Client | None = None
_client_lock = asyncio.Lock()


def _byok_key(job_id: str) -> str:
    return f"multiagent:byok:{job_id}"


async def get_temporal_client() -> Client:
    """懒连接 Temporal 前端（单例；连接失败向上抛，由编排器回退 legacy）."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = await Client.connect(
                    settings.TEMPORAL_ADDRESS,
                    namespace=settings.TEMPORAL_NAMESPACE,
                )
    return _client


async def start_agent_workflow(payload: dict, job_id: str) -> None:
    """以 job_id 作为 workflow id 启动 AgentDagWorkflow."""
    client = await get_temporal_client()
    await client.start_workflow(
        AgentDagWorkflow,
        payload,
        id=job_id,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def query_agent_job(job_id: str) -> dict | None:
    """查询 workflow 当前 Job 快照；不存在/查询被拒返回 None（调用方回退 Redis）."""
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(job_id)
        return await handle.query("get_job")
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询 Temporal 任务状态失败 {}: {}", job_id, exc)
        return None


async def signal_agent_workflow(job_id: str, signal: str, arg=None) -> None:
    client = await get_temporal_client()
    handle = client.get_workflow_handle(job_id)
    if arg is None:
        await handle.signal(signal)
    else:
        await handle.signal(signal, arg)


async def approve_agent_workflow(job_id: str, node_id: str, approved: bool = True) -> None:
    """审批信号：approve_task(node_id, approved)."""
    client = await get_temporal_client()
    handle = client.get_workflow_handle(job_id)
    # Temporal SDK 的 signal() 仅支持单个 arg，用 dict 包装多参数
    await handle.signal("approve_task", {"node_id": str(node_id), "approved": bool(approved)})


async def cancel_agent_workflow(job_id: str, keep_completed: bool = True) -> None:
    await signal_agent_workflow(job_id, "cancel_request", keep_completed)


async def pause_agent_workflow(job_id: str) -> None:
    await signal_agent_workflow(job_id, "pause")


async def resume_agent_workflow(job_id: str) -> None:
    await signal_agent_workflow(job_id, "resume")


# ── BYOK key 临时桥接（Redis，短 TTL）─────────────────────

async def store_byok_key(job_id: str, api_key: str) -> None:
    r = get_redis()
    await r.set(_byok_key(job_id), api_key, ex=settings.TEMPORAL_BYOK_TTL_SECONDS)


async def load_byok_key(job_id: str) -> str | None:
    if not job_id:
        return None
    r = get_redis()
    raw = await r.get(_byok_key(job_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def delete_byok_key(job_id: str) -> None:
    if not job_id:
        return
    r = get_redis()
    await r.delete(_byok_key(job_id))
