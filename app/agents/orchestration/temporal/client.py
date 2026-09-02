"""Temporal 客户端集成层：工作流启动、查询、信号与 BYOK 密钥临时桥接。

BYOK 说明：用户自备 API key 不进入 workflow 输入（会被 Temporal 持久化到
workflow history），改为存 Redis（短 TTL），执行 Activity 时按 job_id 取用，
任务正常结束删除；取消/中断由 TTL 兜底清理。
"""

import asyncio
import json

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


def _llm_config_key(job_id: str) -> str:
    return f"multiagent:llm-config:{job_id}"


def _replan_context_key(job_id: str) -> str:
    return f"multiagent:temporal-replan-context:{job_id}"


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


async def start_manifest_workflow(payload: dict, job_id: str) -> None:
    """Start the rolling manifest workflow on its dedicated task queue."""
    from app.agents.temporal_manifest_workflows import ManifestWorkflow

    client = await get_temporal_client()
    await client.start_workflow(
        ManifestWorkflow,
        payload,
        id=job_id,
        task_queue=settings.TEMPORAL_MANIFEST_TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def start_logical_read_workflow(payload: dict, job_id: str) -> None:
    """在独立队列启动纯读滚动逻辑计划 Workflow。"""
    from app.agents.temporal_logical_read_workflows import LogicalReadWorkflow

    client = await get_temporal_client()
    await client.start_workflow(
        LogicalReadWorkflow,
        payload,
        id=job_id,
        task_queue=settings.TEMPORAL_LOGICAL_READ_TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def start_logical_effects_workflow(payload: dict, job_id: str) -> None:
    """在独立队列启动预声明审批副作用逻辑计划 Workflow。"""
    from app.agents.temporal_logical_effects_workflows import LogicalEffectsWorkflow

    client = await get_temporal_client()
    await client.start_workflow(
        LogicalEffectsWorkflow,
        payload,
        id=job_id,
        task_queue=settings.TEMPORAL_LOGICAL_EFFECTS_TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )


async def signal_manifest_workflow(job_id: str, signal: str, arg=None) -> None:
    """Signal the dedicated manifest workflow without using static DAG APIs."""
    client = await get_temporal_client()
    handle = client.get_workflow_handle(job_id)
    if arg is None:
        await handle.signal(signal)
    else:
        await handle.signal(signal, arg)


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


async def signal_logical_read_workflow(job_id: str, signal: str, arg=None) -> None:
    """向专用纯读逻辑计划 Workflow 发送信号，不复用静态 DAG API。"""
    client = await get_temporal_client()
    handle = client.get_workflow_handle(job_id)
    if arg is None:
        await handle.signal(signal)
    else:
        await handle.signal(signal, arg)


async def signal_logical_effects_workflow(job_id: str, signal: str, arg=None) -> None:
    """向预声明审批副作用逻辑计划 Workflow 发送控制/审批信号。"""
    client = await get_temporal_client()
    handle = client.get_workflow_handle(job_id)
    if arg is None:
        await handle.signal(signal)
    else:
        await handle.signal(signal, arg)


# ── BYOK key 临时桥接（Redis，短 TTL）─────────────────────

async def store_byok_key(job_id: str, api_key: str) -> None:
    r = get_redis()
    await r.set(_byok_key(job_id), api_key, ex=settings.TEMPORAL_BYOK_TTL_SECONDS)


async def store_job_llm_config(job_id: str, config: dict) -> None:
    """Store the frozen model selection in the same short-lived secret bridge."""
    r = get_redis()
    await r.set(_llm_config_key(job_id), json.dumps(config, ensure_ascii=False), ex=settings.TEMPORAL_BYOK_TTL_SECONDS)


async def store_temporal_replan_context(job_id: str, context: dict) -> None:
    """Store non-secret planning inputs for a static Temporal replan Activity."""
    r = get_redis()
    await r.set(
        _replan_context_key(job_id),
        json.dumps(context, ensure_ascii=False, default=str),
        ex=settings.TEMPORAL_BYOK_TTL_SECONDS,
    )


async def load_job_llm_config(job_id: str) -> dict | None:
    if not job_id:
        return None
    try:
        r = get_redis()
        raw = await r.get(_llm_config_key(job_id))
    except Exception:
        # Compatibility with deployments created before the full snapshot
        # bridge: the old key remains a valid credential-only fallback.
        legacy = await load_byok_key(job_id)
        return {"api_key": legacy} if legacy else None
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


async def load_temporal_replan_context(job_id: str) -> dict | None:
    if not job_id:
        return None
    try:
        r = get_redis()
        raw = await r.get(_replan_context_key(job_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


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
    await r.delete(_llm_config_key(job_id))
    await r.delete(_replan_context_key(job_id))
