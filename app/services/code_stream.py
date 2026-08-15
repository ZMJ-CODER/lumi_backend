"""代码生成流式缓冲 —— 后端 LLM 增量 → Redis list → 前端轮询 → 客户端写盘.

设计：
  - writer 生成代码时按 token 增量 push 到 Redis list（key: code_stream:{job_id}:{node_id}）；
  - 前端在任务执行期间轮询 GET /agents/jobs/{id}/stream，拿到增量后通过 Electron IPC
    直接追加写入本地真实文件（首次写入前备份，失败/撤销可恢复）；
  - 流式通道只服务"全文重写"路径；补丁模式输出量小且必须整体应用，不走流式。
  - 任何 Redis 异常静默降级：写盘退化为任务结束后的整体写入（原逻辑），不影响主流程。
"""

from __future__ import annotations

import json
import time

from loguru import logger

from app.core.redis import get_redis

STREAM_PREFIX = "code_stream:"
STREAM_TTL_SECONDS = 600


def _key(job_id: str, node_id: str) -> str:
    return f"{STREAM_PREFIX}{job_id}:{node_id}"


async def _push(job_id: str, node_id: str, payload: dict) -> None:
    try:
        r = get_redis()
        key = _key(job_id, node_id)
        await r.rpush(key, json.dumps(payload, ensure_ascii=False))
        await r.expire(key, STREAM_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CodeStream] 写入缓冲失败（不影响主流程）: {}", exc)


async def start_stream(
    job_id: str,
    node_id: str,
    project_id: str,
    path: str,
    mode: str = "full",
) -> None:
    await _push(
        job_id,
        node_id,
        {
            "type": "start",
            "project_id": str(project_id or ""),
            "path": str(path or ""),
            "mode": mode,
            "ts": time.time(),
        },
    )


async def push_chunk(job_id: str, node_id: str, text: str) -> None:
    if not text:
        return
    await _push(job_id, node_id, {"type": "chunk", "text": text, "ts": time.time()})


async def end_stream(job_id: str, node_id: str, ok: bool = True, error: str = "") -> None:
    await _push(
        job_id,
        node_id,
        {"type": "end", "ok": bool(ok), "error": str(error or ""), "ts": time.time()},
    )


async def read_stream(job_id: str, node_id: str, cursor: int = 0) -> tuple[list[dict], int]:
    """读取自 cursor 之后的流式消息；返回 (chunks, new_cursor)."""
    try:
        r = get_redis()
        key = _key(job_id, node_id)
        items = await r.lrange(key, int(cursor), -1)
        chunks = []
        for raw in items:
            try:
                chunks.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return chunks, int(cursor) + len(items)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CodeStream] 读取缓冲失败: {}", exc)
        return [], int(cursor)
