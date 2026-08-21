"""External logical plans for rolling materialization of ordinary DAGs.

``Job.nodes`` is an execution window, not a durable copy of every planned
node.  The complete plan lives here; a window only contains nodes whose
logical dependencies have already committed.  Completed outputs are retained
as owner-scoped result references, never copied into the Job snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any

from app.agents.orchestration.models import TaskNode, TaskStatus
from app.agents.orchestration.task_routing import RouteChannel, estimate_tokens
from app.core.config import settings


_memory_plans: dict[str, dict[str, Any]] = {}
_memory_lock = asyncio.Lock()


def _owner_key(user_id: str) -> str:
    return hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()[:24]


def _key(user_id: str, plan_id: str) -> str:
    return f"agent:logical-plan:{_owner_key(user_id)}:{plan_id}"


def _ttl() -> int:
    return max(3600, int(settings.AGENT_JOBS_TTL_SECONDS))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _channel_for(node: TaskNode) -> RouteChannel:
    raw = str((node.metadata or {}).get("route_channel") or "")
    try:
        return RouteChannel(raw)
    except ValueError:
        pass
    if node.agent in {"office_script", "office_document"}:
        return RouteChannel.DETERMINISTIC_SCRIPT
    if node.agent in {"retrieval"}:
        return RouteChannel.RAG
    if node.agent in {"direct_llm"}:
        return RouteChannel.DIRECT_LLM
    return RouteChannel.AGENT


def _instruction_for(node: TaskNode) -> str:
    params = node.params or {}
    return str(
        params.get("instruction")
        or params.get("task")
        or params.get("query")
        or node.name
        or node.id
    )


def _node_record(node: TaskNode) -> dict[str, Any]:
    snapshot = node.model_copy(deep=True)
    snapshot.status = TaskStatus.PENDING
    snapshot.result = None
    snapshot.error = None
    snapshot.error_code = None
    snapshot.retries = 0
    snapshot.started_at = None
    snapshot.completed_at = None
    metadata = dict(snapshot.metadata or {})
    metadata.pop("dependency_results", None)
    metadata.pop("result_ref", None)
    snapshot.metadata = metadata
    channel = _channel_for(snapshot)
    return {
        "node": snapshot.model_dump(mode="json"),
        "status": "pending",
        "estimated_tokens": estimate_tokens(_instruction_for(snapshot), channel),
        "result_ref": None,
        "error": "",
        "error_code": "",
        "effect_status": snapshot.effect_status,
    }


async def create_logical_plan(user_id: str, nodes: list[TaskNode]) -> dict[str, Any]:
    """Persist a complete plan while keeping the caller's Job body-free."""
    plan_id = uuid.uuid4().hex
    records = {node.id: _node_record(node) for node in nodes}
    total = sum(int(record["estimated_tokens"]) for record in records.values())
    plan = {
        "version": 1,
        "plan_id": plan_id,
        "created_at": time.time(),
        "updated_at": time.time(),
        "nodes": records,
        "order": [node.id for node in nodes],
        "budget": {
            "limit": int(settings.AGENT_LOGICAL_PLAN_TOKEN_BUDGET),
            "estimated_total": total,
            "reserved": 0,
            "used_estimated": 0,
        },
        "revision": 1,
        "history": [],
    }
    await save_logical_plan(user_id, plan)
    return plan


async def load_logical_plan(user_id: str, plan_id: str) -> dict[str, Any] | None:
    if not plan_id:
        return None
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(_key(user_id, plan_id))
        plan = json.loads(raw) if raw else None
    except Exception:
        async with _memory_lock:
            plan = _memory_plans.get(f"{_owner_key(user_id)}:{plan_id}")
    return json.loads(_json(plan)) if isinstance(plan, dict) else None


async def save_logical_plan(user_id: str, plan: dict[str, Any]) -> None:
    plan["updated_at"] = time.time()
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        raise ValueError("逻辑计划缺少 plan_id")
    raw = _json(plan)
    try:
        from app.core.redis import get_redis

        await get_redis().set(_key(user_id, plan_id), raw, ex=_ttl())
    except Exception:
        async with _memory_lock:
            _memory_plans[f"{_owner_key(user_id)}:{plan_id}"] = json.loads(raw)


def logical_plan_progress(plan: dict[str, Any]) -> dict[str, int]:
    records = list((plan.get("nodes") or {}).values())
    statuses = [str(record.get("status") or "pending") for record in records]
    return {
        "total": len(records),
        "completed": statuses.count("completed"),
        # ``escalated`` and dependency-driven ``skipped`` records cannot
        # safely unlock a successor.  Expose them through the same terminal
        # arbitration counter so the orchestrator can choose L2/L3 instead of
        # silently treating the logical plan as runnable.
        "failed": (
            statuses.count("failed")
            + statuses.count("escalated")
            + statuses.count("skipped")
        ),
        "cancelled": statuses.count("cancelled"),
        "pending": statuses.count("pending") + statuses.count("materialized"),
    }


def _ready_ids(plan: dict[str, Any]) -> list[str]:
    records = plan.get("nodes") or {}
    ready: list[str] = []
    for node_id in list(plan.get("order") or []):
        record = records.get(node_id)
        if not isinstance(record, dict) or record.get("status") != "pending":
            continue
        raw_node = record.get("node") or {}
        deps = list(raw_node.get("depends_on") or [])
        if all(str((records.get(dep_id) or {}).get("status") or "") == "completed" for dep_id in deps):
            ready.append(node_id)
    return ready


def materialize_frontier(plan: dict[str, Any], *, limit: int | None = None) -> list[TaskNode]:
    """Return a bounded ready window and reserve its estimated budget.

    Dependencies remain outside the active DAG.  Their opaque references are
    attached as metadata and resolved only when the new node starts.
    """
    records = plan.get("nodes") or {}
    budget = plan.get("budget") or {}
    size = max(1, int(limit or settings.AGENT_LOGICAL_PLAN_FRONTIER_SIZE))
    selected: list[TaskNode] = []
    reserved = int(budget.get("reserved") or 0)
    used = int(budget.get("used_estimated") or 0)
    ceiling = int(budget.get("limit") or settings.AGENT_LOGICAL_PLAN_TOKEN_BUDGET)
    for node_id in _ready_ids(plan):
        if len(selected) >= size:
            break
        record = records[node_id]
        estimate = int(record.get("estimated_tokens") or 0)
        if used + reserved + estimate > ceiling:
            break
        node = TaskNode.model_validate(record.get("node") or {})
        original_deps = list(node.depends_on)
        refs = {
            dep_id: (records.get(dep_id) or {}).get("result_ref")
            for dep_id in original_deps
            if isinstance((records.get(dep_id) or {}).get("result_ref"), dict)
        }
        node.depends_on = []
        node.status = TaskStatus.PENDING
        node.result = None
        node.error = None
        node.error_code = None
        node.retries = 0
        node.started_at = None
        node.completed_at = None
        node.metadata = {
            **(node.metadata or {}),
            "logical_plan_id": plan.get("plan_id"),
            "logical_node_id": node_id,
            "logical_dependencies": original_deps,
            "logical_dependency_refs": refs,
            "logical_plan_revision": plan.get("revision", 1),
        }
        record["status"] = "materialized"
        selected.append(node)
        reserved += estimate
    budget["reserved"] = reserved
    plan["budget"] = budget
    return selected


async def commit_frontier_results(user_id: str, plan: dict[str, Any], nodes: list[TaskNode]) -> None:
    """Commit a completed execution window into the external logical plan."""
    from app.agents.orchestration.execution_lineage import ensure_node_result_ref

    records = plan.get("nodes") or {}
    budget = plan.get("budget") or {}
    reserved = int(budget.get("reserved") or 0)
    used = int(budget.get("used_estimated") or 0)
    for node in nodes:
        node_id = str((node.metadata or {}).get("logical_node_id") or node.id)
        record = records.get(node_id)
        if not isinstance(record, dict) or record.get("status") not in {"materialized", "pending"}:
            continue
        estimate = int(record.get("estimated_tokens") or 0)
        reserved = max(0, reserved - estimate)
        used += estimate
        status = node.status.value if hasattr(node.status, "value") else str(node.status)
        record["status"] = status
        record["error"] = str(node.error or "")[:500]
        record["error_code"] = str(node.error_code or "")[:120]
        record["effect_status"] = node.effect_status
        if status == TaskStatus.COMPLETED.value:
            record["result_ref"] = await ensure_node_result_ref(user_id, node)
    budget["reserved"] = reserved
    budget["used_estimated"] = used
    plan["budget"] = budget
    await save_logical_plan(user_id, plan)


def replace_unfinished_tail(plan: dict[str, Any], nodes: list[TaskNode], *, reason: str) -> None:
    """Replace only uncommitted logical nodes after orchestrator-approved L3."""
    records = plan.get("nodes") or {}
    completed_ids = [
        node_id for node_id in plan.get("order") or []
        if str((records.get(node_id) or {}).get("status") or "") == "completed"
    ]
    retired = [
        node_id for node_id in plan.get("order") or []
        if str((records.get(node_id) or {}).get("status") or "") != "completed"
    ]
    revision = int(plan.get("revision") or 1) + 1
    prefix = f"replan-{revision}-"
    new_records: dict[str, dict[str, Any]] = {
        node_id: record for node_id, record in records.items()
        if node_id in completed_ids
    }
    id_map: dict[str, str] = {}
    for position, node in enumerate(nodes, start=1):
        old_id = node.id
        node.id = f"{prefix}{position}-{uuid.uuid4().hex[:6]}"
        id_map[old_id] = node.id
    for node in nodes:
        dependencies = [id_map[dep_id] for dep_id in node.depends_on if dep_id in id_map]
        node.depends_on = dependencies or list(completed_ids)
        node.metadata = {**(node.metadata or {}), "plan_revision": revision}
        new_records[node.id] = _node_record(node)
    plan["nodes"] = new_records
    plan["order"] = [*completed_ids, *(node.id for node in nodes)]
    plan["revision"] = revision
    plan["history"] = [
        *(plan.get("history") or [])[-5:],
        {"revision": revision, "reason": reason[:300], "retired_node_ids": retired, "at": time.time()},
    ]
    plan["budget"] = {
        **(plan.get("budget") or {}),
        "reserved": 0,
        "estimated_total": sum(int(record.get("estimated_tokens") or 0) for record in new_records.values()),
    }
