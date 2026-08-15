"""多智能体任务节点实时进度 —— Redis 轻量通道.

Worker 执行节点过程中用 set_progress 持续更新“正在做什么”（如正在阅读哪个文件）；
前端轮询 /agents/jobs/{id} 时，编排器把最新进度文本合并进 node.metadata["progress"]。
进度只用于展示，不影响任务状态机；残留由 TTL（1 小时）自动清理。
"""

from loguru import logger

from app.core.redis import get_redis

_PROGRESS_TTL_SECONDS = 3600


def _key(job_id: str) -> str:
    return f"multiagent:progress:{job_id}"


async def set_progress(job_id: str, node_id: str, text: str) -> None:
    """写入某个节点的最新进度文本（幂等覆盖）."""
    if not job_id or not node_id or not text:
        return
    try:
        r = get_redis()
        key = _key(job_id)
        await r.hset(key, node_id, text)
        await r.expire(key, _PROGRESS_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("写入任务进度失败 {}:{}: {}", job_id, node_id, exc)


async def get_job_progress(job_id: str) -> dict[str, str]:
    """读取任务全部节点的最新进度文本（node_id -> text）."""
    if not job_id:
        return {}
    try:
        r = get_redis()
        raw = await r.hgetall(_key(job_id))
        return {str(k): str(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


async def clear_job_progress(job_id: str) -> None:
    """任务结束清理（可选；未调用由 TTL 兜底）."""
    if not job_id:
        return
    try:
        r = get_redis()
        await r.delete(_key(job_id))
    except Exception:  # noqa: BLE001
        pass
