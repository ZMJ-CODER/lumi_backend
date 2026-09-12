"""操作快照：Job 级「最近一次工作区操作」摘要（刷新后可恢复，不塞正文）。

为什么单独一层（而不是复用事件流）：事件流是**追加**的（用于实时过程气泡），
Job 快照需要的是**最新状态**：某个文件现在是什么版本、审批到哪一步、还能不能撤销。
两者的消费方式不同，混在一起会让前端为了拿"当前状态"去扫全量事件。

存储与能力事件同一套机制（Redis + TTL + 有界），并且：

* 只存安全摘要（operation / logical_path / status / revision / 影响面 / 审批 / 可回滚），
  **不存**文件正文、绝对路径或原始参数；完整 Diff 走受权限保护的接口按需获取；
* Redis 不可用时全部降级为 no-op（操作本身绝不能因为快照失败而失败）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from app.contracts.operations import OperationStatus
from app.contracts.operations.results import OperationResult

_PREFIX = "operation_summary:"
_TTL_SECONDS = 7 * 24 * 3600
_MAX_ENTRIES = 50

#: 快照里允许出现的字段（白名单；与 operation_events 同一安全口径）。
_SUMMARY_FIELDS: tuple[str, ...] = (
    "operation",
    "operation_id",
    "step_id",
    "status",
    "logical_path",
    "target_path",
    "revision",
    "old_revision",
    "new_revision",
    "changed_files",
    "file_count",
    "dir_count",
    "bytes",
    "recursive",
    "permanent",
    "dry_run",
    "approval_state",
    "requires_approval",
    "rollback_available",
    "error_code",
    "error_message",
    "retryable",
    "safe_next_action",
    "provider_id",
    "workspace_id",
    "duration_ms",
    "at",
)


def _key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


def summary_from_result(result: OperationResult) -> dict[str, Any]:
    """``OperationResult`` → 可持久化的安全摘要。"""
    error = result.error
    stats = result.stats or {}
    changed = result.affected_files
    if not changed and result.status is OperationStatus.PENDING_APPROVAL:
        # 待审批还没有产生变化，但前端必须知道"正在等确认的是哪个路径"。
        planned = result.logical_path or result.target_path
        changed = [planned] if planned else []
    return {
        "operation": str(result.kind),
        "operation_id": result.operation_id,
        "step_id": result.step_id,
        "status": str(result.status),
        "logical_path": result.logical_path,
        "target_path": result.target_path,
        "revision": result.new_revision or result.old_revision,
        "old_revision": result.old_revision,
        "new_revision": result.new_revision,
        "changed_files": changed[:50],
        "file_count": int(stats.get("files") or len(result.affected_files)),
        "dir_count": int(stats.get("dirs") or 0),
        "bytes": int(stats.get("bytes") or result.changes.bytes_written),
        "recursive": bool(stats.get("recursive")),
        "permanent": bool(stats.get("permanent")),
        "dry_run": bool(result.dry_run),
        "approval_state": str(result.approval_state),
        "requires_approval": result.requires_approval,
        "rollback_available": result.rollback_available,
        "error_code": error.code if error else "",
        "error_message": error.message if error else "",
        "retryable": bool(error.retryable) if error else False,
        "safe_next_action": error.safe_next_action if error else "",
        "provider_id": result.provider_id,
        "workspace_id": result.workspace_id,
        "duration_ms": int(result.duration_ms),
        "at": time.time(),
    }


def _sanitize(summary: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in _SUMMARY_FIELDS:
        value = summary.get(key)
        if value is None or value == "":
            continue
        if key in {"logical_path", "target_path"}:
            payload[key] = str(value)[:400]
        elif key == "changed_files":
            payload[key] = [str(item) for item in (value or [])][:50]
        else:
            payload[key] = value
    return payload


async def record_operation_summary(result: OperationResult) -> bool:
    """记录一次操作摘要（同一步骤覆盖上一次；失败只记日志）。"""
    job_id = str(result.job_id or "")
    if not job_id:
        return False
    summary = _sanitize(summary_from_result(result))
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        await redis.hset(_key(job_id), str(result.operation_id or result.step_id or "latest"), json.dumps(summary, ensure_ascii=False, default=str))
        await redis.expire(_key(job_id), _TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 快照失败不能影响操作
        logger.debug("[operation] 操作快照写入失败（降级）: {}", str(exc)[:120])
        return False


async def operation_summaries_for_job(job_id: str) -> list[dict[str, Any]]:
    """读取某个 Job 的操作摘要（最新在前，最多 ``_MAX_ENTRIES`` 条）。"""
    target = str(job_id or "")
    if not target:
        return []
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        rows = await redis.hvals(_key(target))
    except Exception:  # noqa: BLE001
        return []
    summaries: list[dict[str, Any]] = []
    for raw in rows or []:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get("operation"):
            summaries.append(item)
    summaries.sort(key=lambda item: float(item.get("at") or 0), reverse=True)
    return summaries[:_MAX_ENTRIES]


async def operation_summary_view(job_id: str) -> dict[str, Any]:
    """给 Job.run_view 用的聚合视图（前端"操作面板"直接消费）。"""
    summaries = await operation_summaries_for_job(job_id)
    pending = [item for item in summaries if item.get("requires_approval")]
    changed: list[str] = []
    for item in summaries:
        for path in item.get("changed_files") or []:
            if path not in changed:
                changed.append(path)
    return {
        "operations": summaries,
        "latest": summaries[0] if summaries else None,
        "changed_files": changed[:50],
        "approval_state": (pending[0].get("approval_state") if pending else "not_required"),
        "rollback_available": any(
            bool(item.get("rollback_available")) for item in summaries
        ),
        "no_change": bool(
            summaries and summaries[0].get("status") == OperationStatus.NO_CHANGE.value
        ),
    }


__all__ = [
    "operation_summaries_for_job",
    "operation_summary_view",
    "record_operation_summary",
    "summary_from_result",
]
