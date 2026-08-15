"""任务记忆：一个任务（job）内共享的工作记忆（已读文件/已做决策/中间结果）.

存储：Redis hash `task_memory:{job_id}`。任务内节点/技能可读写；
节点完成后自动沉淀摘要，最终汇总时把任务记忆一并交给 LLM。
"""

from __future__ import annotations

from app.core.redis import get_redis

_TTL = 6 * 3600  # 6 小时


def _key(job_id: str) -> str:
    return f"task_memory:{job_id}"


async def remember(job_id: str, key: str, value: str) -> None:
    """写入一条任务记忆（失败静默）."""
    try:
        r = get_redis()
        k = str(key or "")[:80]
        if k:
            await r.hset(_key(job_id), k, str(value or "")[:2000])
            await r.expire(_key(job_id), _TTL)
    except Exception:  # noqa: BLE001
        pass


async def recall(job_id: str) -> dict[str, str]:
    """读取任务记忆快照."""
    try:
        r = get_redis()
        return await r.hgetall(_key(job_id))
    except Exception:  # noqa: BLE001
        return {}


def format_memory(mem: dict[str, str], limit: int = 20) -> str:
    """任务记忆 → 提示词文本."""
    items = list((mem or {}).items())[:limit]
    if not items:
        return ""
    return "\n".join(f"- {k}：{v[:300]}" for k, v in items)
