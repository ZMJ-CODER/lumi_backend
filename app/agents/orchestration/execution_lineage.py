"""Execution lineage, compact node-result references, and audit spans.

The Job snapshot is intentionally not used as a replay payload.  A forked
execution retains only a result reference (opaque id + content hash) for its
successful prefix.  The sanitized body stays in the result store and is read
only by a later dependent node at execution time.
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


async def persist_result_ref(user_id: str, result: dict | None) -> dict[str, str] | None:
    """Store a sanitized node result and return a body-free reference."""
    if not result:
        return None
    from app.agents.orchestration.context import sanitize_dependency_result

    body = sanitize_dependency_result(result)
    raw = _json(body)
    result_id = uuid.uuid4().hex
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    record = {"sha256": digest, "body": body, "created_at": time.time()}
    try:
        from app.core.redis import get_redis

        await get_redis().set(_result_key(user_id, result_id), _json(record), ex=_ttl())
    except Exception:
        async with _memory_lock:
            _memory_results[f"{_owner_key(user_id)}:{result_id}"] = record
    return {"id": result_id, "sha256": digest}


async def resolve_result_ref(user_id: str, result_ref: dict | None) -> dict | None:
    """Resolve a reference only for the owning user's dependent node."""
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
    if not isinstance(record, dict) or str(record.get("sha256") or "") != expected:
        return None
    body = record.get("body")
    if not isinstance(body, dict):
        return None
    actual = hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
    return body if actual == expected else None


async def ensure_node_result_ref(user_id: str, node) -> dict[str, str] | None:
    """Return a valid node reference, creating one for older job snapshots."""
    metadata = dict(getattr(node, "metadata", {}) or {})
    existing = metadata.get("result_ref")
    if await resolve_result_ref(user_id, existing):
        return existing
    created = await persist_result_ref(user_id, getattr(node, "result", None))
    if created:
        metadata["result_ref"] = created
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
