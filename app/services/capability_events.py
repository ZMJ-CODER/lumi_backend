"""阶段 2：能力状态事件流（SSE 新增事件，**不改**普通聊天 delta 流程）。

与 ``app/services/office_stream.py`` 同一套机制（Redis list + 游标 + 短 TTL），
因为要解决的问题相同：过程状态在任务结束前就要抵达前端，且断线/刷新不能重复消费。

事件类型与前端 ``src/types/plugins.ts`` 的 ``CapabilityEventType`` 逐字一致：
``capability_requested`` / ``waiting_provider`` / ``provider_connected`` /
``provider_disconnected`` / ``capability_started`` / ``capability_completed`` /
``capability_failed`` / ``plugin_health_changed`` / ``approval_required``。

两条约束：

* **结构性状态不被压成"工具失败"**：``provider_offline`` / ``lease_expired`` 等以
  ``error_code`` 原样透传，前端据此给"重连/安装 Provider"入口；
* 事件必须**有界且安全**：只放能力名/Provider/版本/状态/错误码，不放参数与正文。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityResult,
    CapabilityStatus,
    capability_status_for,
)

_PREFIX = "capability_events:"
_TTL_SECONDS = 900
_MAX_EVENTS_PER_JOB = 200

#: 事件类型常量（与前端联合类型一致；"approval_required" 复用既有审批语义）。
CAPABILITY_EVENT_REQUESTED = "capability_requested"
CAPABILITY_EVENT_WAITING_PROVIDER = "waiting_provider"
CAPABILITY_EVENT_PROVIDER_CONNECTED = "provider_connected"
CAPABILITY_EVENT_PROVIDER_DISCONNECTED = "provider_disconnected"
CAPABILITY_EVENT_STARTED = "capability_started"
CAPABILITY_EVENT_COMPLETED = "capability_completed"
CAPABILITY_EVENT_FAILED = "capability_failed"
CAPABILITY_EVENT_PLUGIN_HEALTH_CHANGED = "plugin_health_changed"
CAPABILITY_EVENT_APPROVAL_REQUIRED = "approval_required"

#: 能力事件类型全集（SSE 出口据此补统一过程字段）。
CAPABILITY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        CAPABILITY_EVENT_REQUESTED,
        CAPABILITY_EVENT_WAITING_PROVIDER,
        CAPABILITY_EVENT_PROVIDER_CONNECTED,
        CAPABILITY_EVENT_PROVIDER_DISCONNECTED,
        CAPABILITY_EVENT_STARTED,
        CAPABILITY_EVENT_COMPLETED,
        CAPABILITY_EVENT_FAILED,
        CAPABILITY_EVENT_PLUGIN_HEALTH_CHANGED,
        CAPABILITY_EVENT_APPROVAL_REQUIRED,
    }
)

#: 事件里允许出现的字段（白名单：防止把参数/正文写进状态流）。
_ALLOWED_FIELDS: tuple[str, ...] = (
    "capability",
    "provider_id",
    "provider_version",
    "contract_version",
    "device_id",
    "workspace_id",
    "conversation_id",
    # 实际执行来源（位置 + 运行方式 + 兼容派生值）：前端展示"隔离 Worker · 服务端/客户端"。
    "execution_plane",
    "runtime_kind",
    "executor_type",
    "plugin_id",
    "plugin_version",
    "skill_id",
    "status",
    "health_status",
    "error_code",
    "error",
    "retryable",
    "requires_approval",
    "approval_state",
    "trace_id",
    "call_id",
    "sequence",
    "occurred_at",
)


def _key(job_id: str) -> str:
    return f"{_PREFIX}{job_id}"


def build_capability_event(
    event_type: str,
    *,
    job_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """构造一条能力状态事件（白名单过滤 + 敏感度安全）。"""
    payload: dict[str, Any] = {
        "type": str(event_type),
        "job_id": str(job_id or ""),
        "occurred_at": time.time(),
    }
    for key in _ALLOWED_FIELDS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        if key == "error" and isinstance(value, dict):
            # 错误只带稳定码与可重试标记，不带细节正文。
            payload["error"] = {
                "code": str(value.get("code") or ""),
                "message": str(value.get("message") or "")[:300],
                "retryable": bool(value.get("retryable")),
            }
            continue
        payload[key] = value
    return payload


async def publish_capability_event(event_type: str, *, job_id: str = "", **fields: Any) -> bool:
    """发布一条能力状态事件（失败只记日志，绝不影响调用主流程）。"""
    event = build_capability_event(event_type, job_id=job_id, **fields)
    target = str(job_id or "")
    if not target:
        logger.debug("[capability] 无 job_id，事件仅记录不落流: {}", event.get("type"))
        return False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        # 事件里已带 occurred_at（epoch），列表只追加，读取端按游标消费。
        await redis.rpush(_key(target), json.dumps(event, ensure_ascii=False, default=str))
        await redis.ltrim(_key(target), -_MAX_EVENTS_PER_JOB, -1)
        await redis.expire(_key(target), _TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - 状态流不可用不能影响能力调用
        logger.debug("[capability] 事件落流失败（降级）: {}", str(exc)[:120])
        return False


async def read_capability_events(job_id: str, cursor: int = 0) -> tuple[list[dict], int]:
    """按游标读取能力状态事件（与 ``office_stream.read_deltas`` 同形状）。"""
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


def events_for_result(
    result: CapabilityResult,
    *,
    capability: str = "",
    job_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """把一次调用结果折叠成**一个**状态事件。

    ``status`` 用契约词表；**事件类型**也按语义分派：

    * 成功 → ``capability_completed``
    * 需要审批 → ``approval_required``（前端只对这个类型置 ``waiting_approval``，
      若塞进 ``capability_failed`` 会被渲染成普通失败，审批入口就没了）
    * 其余失败按错误码映射到 ``waiting_approval`` / ``denied`` / ``unavailable`` / ``failed``

    执行来源（``execution_plane`` / ``runtime_kind`` / ``executor_type``）随事件一起发，
    前端不必从 ``deployment`` 猜"谁在执行"。
    """
    capability_name = str(capability or result.capability or "")
    payload: dict[str, Any] = {
        "capability": capability_name,
        "provider_id": result.provider_id,
        "contract_version": int(result.contract_version or 1),
        "trace_id": result.trace_id,
        "request_id": result.request_id,
        "execution_plane": str(result.plane()) if result.execution_plane else "",
        "runtime_kind": str(result.runtime()) if result.runtime_kind else "",
        "executor_type": result.executor_type() if result.execution_plane or result.runtime_kind else "",
        **fields,
    }
    if result.ok:
        return build_capability_event(
            CAPABILITY_EVENT_COMPLETED,
            job_id=job_id,
            status=str(CapabilityStatus.COMPLETED),
            **payload,
        )
    status = capability_status_for(ok=False, error_code=result.error_code)
    envelope = result.error
    if status is CapabilityStatus.WAITING_APPROVAL:
        # 前端按事件类型置 waiting_approval；用 approval_required 才不会显示成失败。
        return build_capability_event(
            CAPABILITY_EVENT_APPROVAL_REQUIRED,
            job_id=job_id,
            status=str(status),
            error_code=result.error_code or CapabilityErrorCode.APPROVAL_REQUIRED.value,
            error=envelope.model_dump(mode="json") if envelope is not None else None,
            retryable=result.retryable,
            requires_approval=True,
            approval_state="pending",
            **payload,
        )
    return build_capability_event(
        CAPABILITY_EVENT_FAILED,
        job_id=job_id,
        status=str(status),
        error_code=result.error_code or CapabilityErrorCode.FAILED.value,
        error=envelope.model_dump(mode="json") if envelope is not None else None,
        retryable=result.retryable,
        requires_approval=False,
        **payload,
    )


__all__ = [
    "CAPABILITY_EVENT_APPROVAL_REQUIRED",
    "CAPABILITY_EVENT_COMPLETED",
    "CAPABILITY_EVENT_FAILED",
    "CAPABILITY_EVENT_PLUGIN_HEALTH_CHANGED",
    "CAPABILITY_EVENT_PROVIDER_CONNECTED",
    "CAPABILITY_EVENT_PROVIDER_DISCONNECTED",
    "CAPABILITY_EVENT_REQUESTED",
    "CAPABILITY_EVENT_STARTED",
    "CAPABILITY_EVENT_TYPES",
    "CAPABILITY_EVENT_WAITING_PROVIDER",
    "build_capability_event",
    "events_for_result",
    "publish_capability_event",
    "read_capability_events",
]
