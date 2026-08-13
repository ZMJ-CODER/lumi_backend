"""Temporal Activities —— 节点执行与任务清理.

Activity 是 Temporal 中做副作用的地方：LLM 调用、技能执行、DB/Redis 读写
都在这里发生，Workflow 保持确定性。节点执行语义与 legacy dag.py 一致：
worker.execute → 质检 → React 重试（最多 max_retries 次）。
"""

import asyncio
import json

from loguru import logger
from temporalio import activity
import temporalio.exceptions

from app.agents.orchestration.models import TaskNode, TaskStatus
from app.agents.orchestration.review import get_reviewer
from app.agents.orchestration.workers import WORKERS, WorkerContext
from app.agents.orchestration.temporal.client import load_byok_key


def _json_safe(obj):
    """保证 Activity 返回值可被 Temporal JSON 数据转换器序列化."""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {
            "status": "failed",
            "result": None,
            "error": "活动结果序列化失败",
            "error_code": "EXEC_ERROR",
            "retries": 0,
        }


@activity.defn
async def execute_node_activity(payload: dict) -> dict:
    """执行单个任务节点：定位 worker → 执行 → 质检 → React 重试."""
    node_data = payload.get("node") or {}
    job_id = str(payload.get("job_id") or "")
    user_id = str(payload.get("user_id") or "")
    scene = str(payload.get("scene") or "office")
    cfg = payload.get("config") or {}
    timeout = int(cfg.get("node_timeout_seconds") or 300)
    max_retries = int(cfg.get("node_max_retries") or 2)

    worker = WORKERS.get(node_data.get("agent"))
    if worker is None:
        return {
            "status": "failed",
            "result": None,
            "error": f"未注册的执行 agent: {node_data.get('agent')}",
            "error_code": "AGENT_NOT_FOUND",
            "retries": 0,
        }

    node = TaskNode.model_validate(node_data)
    review = get_reviewer()
    llm_api_key = await load_byok_key(job_id) if job_id else None
    ctx = WorkerContext(
        user_id=user_id, job_id=job_id, scene=scene, llm_api_key=llm_api_key
    )

    for attempt in range(max_retries + 1):
        if attempt:
            node.retries = attempt
            node.status = TaskStatus.RETRYING
        node.status = TaskStatus.RUNNING
        node.error = None
        node.error_code = None
        error = None
        error_code = None
        result = None
        try:
            result = await asyncio.wait_for(worker.execute(node, ctx), timeout=timeout)
            if isinstance(result, dict) and result.get("success") is False:
                error = str(result.get("error") or "执行失败")
                error_code = str(result.get("error_code") or "EXEC_ERROR")
        except asyncio.CancelledError:
            return {
                "status": "interrupted",
                "result": None,
                "error": "任务被用户终止",
                "error_code": "INTERRUPTED",
                "retries": attempt,
            }
        except temporalio.exceptions.CancelledError:
            return {
                "status": "interrupted",
                "result": None,
                "error": "任务被用户终止",
                "error_code": "INTERRUPTED",
                "retries": attempt,
            }
        except asyncio.TimeoutError:
            error = f"执行超时（>{timeout}s）"
            error_code = "TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            logger.warning("节点 {} 执行异常: {}", node.id, exc)
            error = str(exc)
            error_code = "EXEC_ERROR"

        if error:
            if attempt < max_retries:
                continue
            return {
                "status": "failed",
                "result": None,
                "error": error,
                "error_code": error_code,
                "retries": attempt,
            }

        # 质检（不通过则重做，最多 max_retries 次）
        verdict = await review.review(node, result, ctx)
        if verdict.approved:
            return _json_safe({"status": "completed", "result": result, "retries": attempt})
        if attempt < max_retries:
            continue
        return {
            "status": "failed",
            "result": None,
            "error": f"质检未通过: {verdict.feedback}",
            "error_code": "REVIEW_REJECTED",
            "retries": attempt,
        }

    return {
        "status": "failed",
        "result": None,
        "error": "执行失败",
        "error_code": "EXEC_ERROR",
        "retries": max_retries,
    }


@activity.defn
async def cleanup_job_secrets_activity(job_id: str) -> None:
    """任务正常结束时删除 BYOK 临时 key（取消/中断路径由 TTL 兜底清理）."""
    if job_id:
        from app.agents.orchestration.temporal.client import delete_byok_key

        await delete_byok_key(job_id)
