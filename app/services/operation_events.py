"""操作事件流：``operation_started`` / ``operation_preview`` / ``approval_required`` /
``operation_completed`` / ``operation_failed`` / ``operation_rolled_back``。

与能力事件（``capability_events``）同一套机制（Redis list + 游标 + TTL + 白名单），
但**只承载工作区操作的安全摘要**：

* 只放 operation / logical_path / status / revision / 影响面计数 / 审批状态 / 错误码；
* **不放**原始参数、绝对路径、文件正文（完整 Diff 走受权限保护的接口按需获取）；
* 每条事件都带 ``job_id`` / ``step_id`` / ``operation_id`` / ``sequence``，刷新后可恢复。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from app.contracts.operations.common import OperationKind, OperationStatus
from app.contracts.operations.results import OperationResult

_PREFIX = "operation_events:"
_TTL_SECONDS = 900
_MAX_EVENTS_PER_JOB = 300

OPERATION_EVENT_STARTED = "operation_started"
OPERATION_EVENT_PREVIEW = "operation_preview"
OPERATION_EVENT_APPROVAL_REQUIRED = "approval_required"
OPERATION_EVENT_COMPLETED = "operation_completed"
OPERATION_EVENT_FAILED = "operation_failed"
OPERATION_EVENT_ROLLED_BACK = "operation_rolled_back"

#: 全部操作事件类型（SSE 出口据此补统一过程字段）。
OPERATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        OPERATION_EVENT_STARTED,
        OPERATION_EVENT_PREVIEW,
        OPERATION_EVENT_APPROVAL_REQUIRED,
        OPERATION_EVENT_COMPLETED,
        OPERATION_EVENT_FAILED,
        OPERATION_EVENT_ROLLED_BACK,
    }
)

#: 展示标题（与能力事件同形状，前端无需按 operation 名猜）。
OPERATION_EVENT_TITLE: dict[str, str] = {
    OPERATION_EVENT_STARTED: "开始工作区操作",
    OPERATION_EVENT_PREVIEW: "操作预览",
    OPERATION_EVENT_APPROVAL_REQUIRED: "等待确认",
    OPERATION_EVENT_COMPLETED: "操作完成",
    OPERATION_EVENT_FAILED: "操作未完成",
    OPERATION_EVENT_ROLLED_BACK: "操作已回滚",
}

#: 事件字段白名单（防止把参数/正文写进状态流）。
_ALLOWED_FIELDS: tuple[str, ...] = (
    "operation",
    "operation_id",
    "step_id",
    "logical_path",
    "target_path",
    "status",
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
    "lease_id",
    "workspace_id",
    "conversation_id",
    "duration_ms",
    "sequence",
)


def _key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


def build_operation_event(
    event_type: str,
    *,
    job_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """构造一条操作事件（白名单过滤；路径只允许工作区相对路径）。"""
    payload: dict[str, Any] = {
        "type": str(event_type),
        "job_id": str(job_id or ""),
        "occurred_at": time.time(),
    }
    for key in _ALLOWED_FIELDS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        if key in {"logical_path", "target_path"}:
            payload[key] = str(value)[:400]
            continue
        if key == "changed_files":
            rows = [str(item) for item in (value if isinstance(value, (list, tuple)) else [])]
            payload[key] = rows[:50]
            continue
        payload[key] = value
    return payload


async def publish_operation_event(event_type: str, *, job_id: str = "", **fields: Any) -> bool:
    """发布一条操作事件（失败只记日志，绝不影响操作本身）。"""
    event = build_operation_event(event_type, job_id=job_id, **fields)
    target = str(job_id or "")
    if not target:
        logger.debug("[operation] 无 job_id，事件仅记录不落流: {}", event.get("type"))
        return False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        await redis.rpush(_key(target), json.dumps(event, ensure_ascii=False, default=str))
        await redis.ltrim(_key(target), -_MAX_EVENTS_PER_JOB, -1)
        await redis.expire(_key(target), _TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 状态流不可用不能影响操作
        logger.debug("[operation] 事件落流失败（降级）: {}", str(exc)[:120])
        return False


async def read_operation_events(job_id: str, cursor: int = 0) -> tuple[list[dict], int]:
    """按游标读取操作事件（与 ``read_capability_events`` 同形状）。"""
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        items = await redis.lrange(_key(job_id), max(0, int(cursor)), -1)
    except Exception:  # noqa: BLE001
        return [], max(0, int(cursor))
    events: list[dict] = []
    for raw in items:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type"):
            events.append(event)
    return events, max(0, int(cursor)) + len(items)


def event_type_for_result(result: OperationResult) -> str:
    """操作结果 → 事件类型（状态与事件类型一一对应，不让前端猜）。"""
    if result.status is OperationStatus.PENDING_APPROVAL:
        return OPERATION_EVENT_APPROVAL_REQUIRED
    if result.status in {OperationStatus.DENIED, OperationStatus.FAILED}:
        return OPERATION_EVENT_FAILED
    return OPERATION_EVENT_COMPLETED


def event_fields_for_result(result: OperationResult) -> dict[str, Any]:
    """结果 → 事件字段（安全摘要；不含正文与参数）。"""
    error = result.error
    stats = result.stats or {}
    status_value = str(result.status)
    if result.status is OperationStatus.NO_CHANGE:
        # 前端按"完成但无变化"渲染：状态值保持契约原样，事件类型仍是 completed。
        status_value = OperationStatus.NO_CHANGE.value
    changed = result.affected_files
    if not changed and result.status is OperationStatus.PENDING_APPROVAL:
        # 待审批尚未改动工作区，但事件里必须带"正在等确认的路径"。
        planned = result.logical_path or result.target_path
        changed = [planned] if planned else []
    fields: dict[str, Any] = {
        "operation": str(result.kind),
        "operation_id": result.operation_id,
        "step_id": result.step_id,
        "logical_path": result.logical_path,
        "target_path": result.target_path,
        "status": status_value,
        "revision": result.new_revision or result.old_revision,
        "old_revision": result.old_revision,
        "new_revision": result.new_revision,
        "changed_files": changed,
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
        "lease_id": result.lease_id,
        "workspace_id": result.workspace_id,
        "conversation_id": result.conversation_id,
        "duration_ms": int(result.duration_ms),
    }
    if result.kind is OperationKind.MOVE and not result.target_path:
        fields["target_path"] = ""
    return fields


async def publish_operation_result(result: OperationResult) -> bool:
    """按结果发布一条操作事件（含 preview/started 之外的所有终态）。"""
    return await publish_operation_event(
        event_type_for_result(result),
        job_id=result.job_id,
        **event_fields_for_result(result),
    )


__all__ = [
    "OPERATION_EVENT_APPROVAL_REQUIRED",
    "OPERATION_EVENT_COMPLETED",
    "OPERATION_EVENT_FAILED",
    "OPERATION_EVENT_PREVIEW",
    "OPERATION_EVENT_ROLLED_BACK",
    "OPERATION_EVENT_STARTED",
    "OPERATION_EVENT_TITLE",
    "OPERATION_EVENT_TYPES",
    "build_operation_event",
    "event_fields_for_result",
    "event_type_for_result",
    "publish_operation_event",
    "publish_operation_result",
    "read_operation_events",
]
