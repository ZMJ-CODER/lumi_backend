"""执行谱系、紧凑节点结果引用与审计范围。

The Job snapshot is intentionally not used as a replay payload.  A forked
execution retains only a result reference (opaque id + content hash) for its
successful prefix.  The sanitized body stays in the result store and is read
only by a later dependent node at execution time.

结果读写统一收口到 :mod:`app.services.result_store`（方案 §1）：

* ``RESULT_STORE_V2`` 关闭时仍走原来的"Redis 键 + 进程内存兜底"；
* 打开后按**序列化字节数**分层（Redis / 本地 / Blob），引用携带
  ``schema_version`` 与 ``expires_at``，读取先校验归属、过期与 sha256。

对外形状保持不变：``persist_result_ref`` 只返回 ``{"id", "sha256"}``（旧读取方
不需要改），完整引用只在内部使用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from app.core.config import settings


_memory_results: dict[str, dict[str, Any]] = {}
_memory_spans: dict[str, list[dict[str, Any]]] = {}
_memory_lock = asyncio.Lock()


def _owner_key(user_id: str) -> str:
    return hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()[:24]


def _result_key(user_id: str, result_id: str) -> str:
    return f"agent:execution:result:{_owner_key(user_id)}:{result_id}"


def _span_key(execution_id: str) -> str:
    return f"agent:execution:spans:{execution_id}"


def _ttl() -> int:
    return max(
        3600,
        int(settings.AGENT_JOBS_TTL_SECONDS),
        int(settings.AGENT_RESULT_REF_TTL_SECONDS),
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def unified_result_store_enabled() -> bool:
    """结果是否走统一 ResultStore（``RESULT_STORE_V2``，默认关闭）。"""
    try:
        from app.core.feature_flags import feature_enabled

        return feature_enabled("RESULT_STORE_V2")
    except Exception:  # noqa: BLE001 - 开关不可用时保持旧路径（保守）
        return False


async def _save_via_result_store(
    user_id: str,
    result: dict | None,
    *,
    job_id: str = "",
    step_id: str = "",
    tool_name: str = "",
    schema_name: str = "execution_result",
    schema_version: int = 1,
) -> dict[str, str] | None:
    from app.services.result_store import SaveResultRequest, get_result_store

    receipt = await get_result_store().save(
        SaveResultRequest(
            result=result,
            user_id=user_id,
            job_id=job_id,
            step_id=step_id,
            tool_name=tool_name,
            schema_name=schema_name,
            schema_version=schema_version,
            ttl_seconds=_ttl(),
        )
    )
    return receipt.minimal_ref if receipt is not None else None


async def persist_result_ref(
    user_id: str,
    result: dict | None,
    *,
    job_id: str = "",
    step_id: str = "",
    tool_name: str = "",
    schema_name: str = "execution_result",
    schema_version: int = 1,
) -> dict[str, str] | None:
    """Store a sanitized node result and return a body-free reference."""
    if not result:
        return None
    if unified_result_store_enabled():
        saved = await _save_via_result_store(
            user_id,
            result,
            job_id=job_id,
            step_id=step_id,
            tool_name=tool_name,
            schema_name=schema_name,
            schema_version=schema_version,
        )
        if saved:
            return saved
    from app.agents.orchestration.context import sanitize_dependency_result

    body = sanitize_dependency_result(result)
    raw = _json(body)
    result_id = uuid.uuid4().hex
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # 持久化投影：附加一份"可恢复的最小快照"（白名单字段 + 分页指针 + 产物引用，
    # 不含正文）。恢复/fork/滚动窗口只用它就能决定下一步，避免先拉正文。
    from app.contracts.projections import result_storage

    storage = result_storage(body)
    record = {
        "sha256": digest,
        "body": body,
        "storage": storage,
        "created_at": time.time(),
        # 方案 §1.3：引用必须携带**存储时**的 payload 结构版本，读取时按它解析。
        "schema_name": str(schema_name or "execution_result"),
        "schema_version": int(schema_version or 1),
    }
    try:
        from app.core.redis import get_redis

        await get_redis().set(_result_key(user_id, result_id), _json(record), ex=_ttl())
    except Exception:
        async with _memory_lock:
            _memory_results[f"{_owner_key(user_id)}:{result_id}"] = record
    return {"id": result_id, "sha256": digest}


async def resolve_result_storage(user_id: str, result_ref: dict | None) -> dict | None:
    """只取持久化投影（最小快照），不加载正文；用于恢复/fork 的前置判断。"""
    if unified_result_store_enabled():
        from app.services.result_store import resolve_result_storage as _resolve_storage

        stored = await _resolve_storage(user_id, result_ref)
        if isinstance(stored, dict):
            return stored
    record = await _load_record(user_id, result_ref, verify=False)
    if not isinstance(record, dict):
        return None
    storage = record.get("storage")
    return dict(storage) if isinstance(storage, dict) else None


def _sha256_of(body: dict) -> str:
    return hashlib.sha256(_json(body).encode("utf-8")).hexdigest()


async def _load_record(user_id: str, result_ref: dict | None, *, verify: bool = True) -> dict | None:
    """读取结果记录；``verify`` 为真时校验 sha256（正文完整性）。"""
    if not isinstance(result_ref, dict):
        return None
    result_id = str(result_ref.get("id") or "")
    expected = str(result_ref.get("sha256") or "")
    if not result_id or not expected:
        return None
    record = None
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(_result_key(user_id, result_id))
        record = json.loads(raw) if raw else None
    except Exception:
        async with _memory_lock:
            record = _memory_results.get(f"{_owner_key(user_id)}:{result_id}")
    if not isinstance(record, dict):
        return None
    if not verify:
        return record if str(record.get("sha256") or "") == expected else None
    body = record.get("body")
    if not isinstance(body, dict):
        return None
    return record if _sha256_of(body) == expected else None


async def resolve_result_ref(user_id: str, result_ref: dict | None) -> dict | None:
    """Resolve a reference only for the owning user's dependent node."""
    if unified_result_store_enabled():
        from app.services.result_store import resolve_result as _resolve

        body = await _resolve(user_id, result_ref)
        if body is None:
            return None
        # 与旧路径一致：校验请求方给的 sha256（防篡改引用）。
        expected = str((result_ref or {}).get("sha256") or "")
        if expected and _sha256_of(body) != expected:
            return None
        return body
    record = await _load_record(user_id, result_ref, verify=True)
    if not isinstance(record, dict):
        return None
    body = record.get("body")
    return body if isinstance(body, dict) else None


async def ensure_node_result_ref(user_id: str, node) -> dict[str, str] | None:
    """Return a valid node reference, creating one for older job snapshots."""
    metadata = dict(getattr(node, "metadata", {}) or {})
    existing = metadata.get("result_ref")
    if await resolve_result_ref(user_id, existing):
        return existing
    created = await persist_result_ref(
        user_id,
        getattr(node, "result", None),
        job_id=str((metadata.get("job_id") if isinstance(metadata, dict) else "") or ""),
        step_id=str(getattr(node, "id", "") or ""),
        tool_name=str((getattr(node, "params", {}) or {}).get("preferred_tool") or ""),
    )
    if created:
        metadata["result_ref"] = created
        # 持久化投影随之挂在节点上：断线恢复/前端展示不必再解析正文。
        storage = await resolve_result_storage(user_id, created)
        if storage:
            metadata["result_storage"] = storage
        node.metadata = metadata
    return created


async def record_node_span(
    *,
    execution_id: str,
    job_id: str,
    node,
    event: str,
) -> None:
    """Append a compact, redacted lifecycle event for operations and replay."""
    if not execution_id:
        return
    metadata = getattr(node, "metadata", {}) or {}
    params = getattr(node, "params", {}) or {}
    result = getattr(node, "result", {}) or {}
    tool_metadata = result.get("tool_metadata") if isinstance(result, dict) else None
    if not isinstance(tool_metadata, dict):
        tool_metadata = metadata.get("tool_metadata") if isinstance(metadata, dict) else None
    entry = {
        "at": time.time(),
        "execution_id": execution_id,
        "job_id": job_id,
        "node_id": str(getattr(node, "id", "")),
        "event": event,
        "agent": str(getattr(node, "agent", ""))[:80],
        "tool": str(result.get("tool") or params.get("preferred_tool") or "")[:100],
        "input_sha256": hashlib.sha256(_json(params).encode("utf-8")).hexdigest(),
        "result_ref": metadata.get("result_ref"),
        "status": str(getattr(getattr(node, "status", ""), "value", getattr(node, "status", ""))),
        "error_code": str(getattr(node, "error_code", "") or "")[:120],
        "effect_status": getattr(node, "effect_status", None),
        "tool_metadata": {
            key: tool_metadata.get(key)
            for key in ("document_selection", "selection_traces")
            if tool_metadata.get(key)
        } if isinstance(tool_metadata, dict) and any(tool_metadata.get(key) for key in ("document_selection", "selection_traces")) else None,
    }
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        key = _span_key(execution_id)
        await redis.rpush(key, _json(entry))
        await redis.ltrim(key, -2000, -1)
        await redis.expire(key, _ttl())
    except Exception:
        async with _memory_lock:
            values = _memory_spans.setdefault(execution_id, [])
            values.append(entry)
            del values[:-2000]


async def list_node_spans(execution_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return user-safe span metadata; prompt/result bodies are never here."""
    if not execution_id:
        return []
    limit = max(1, min(int(limit), 500))
    try:
        from app.core.redis import get_redis

        raw_items = await get_redis().lrange(_span_key(execution_id), -limit, -1)
        return [json.loads(item) for item in raw_items if item]
    except Exception:
        async with _memory_lock:
            return list(_memory_spans.get(execution_id, [])[-limit:])


__all__ = [
    "ensure_node_result_ref",
    "list_node_spans",
    "persist_result_ref",
    "record_node_span",
    "resolve_result_ref",
    "resolve_result_storage",
    "unified_result_store_enabled",
]
