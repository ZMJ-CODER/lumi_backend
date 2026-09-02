"""用于常规 DAG 滚动物化的外部逻辑计划。

``Job.nodes`` 是执行窗口，而非所有计划节点的持久副本。完整计划保存在这里；
一个窗口只包含逻辑依赖已提交的节点。完成输出仅保留为按所有者隔离的结果引用，
绝不复制进 Job 快照。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from typing import Any

from lumi_orch import ExpansionSlot
from lumi_orch.logical_plan import (
    logical_plan_progress as _kernel_logical_plan_progress,
    select_budgeted_frontier,
)

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


def logical_plan_execution_fingerprint(plan: dict[str, Any]) -> str:
    """返回逻辑计划中不可变节点定义的校验指纹。

    状态、结果引用、预算消耗和更新时间都不参与计算。这样滚动前沿可以持续
    提交结果，但任何对尚未执行节点定义的意外修改都会在执行前被发现。
    """
    records = plan.get("nodes") or {}
    canonical = {
        "version": int(plan.get("version") or 1),
        "plan_id": str(plan.get("plan_id") or ""),
        "order": [str(node_id) for node_id in (plan.get("order") or [])],
        "nodes": {
            str(node_id): {
                "node": (record or {}).get("node") or {},
                "estimated_tokens": int((record or {}).get("estimated_tokens") or 0),
            }
            for node_id, record in sorted(records.items())
        },
        # 插槽定义属于骨架计划的一部分，必须纳入指纹；运行时状态则不纳入，
        # 否则一次合法补图会让已持久化的执行指纹失效。
        "slots": {
            str(slot_id): {
                "id": str(raw.get("id") or ""),
                "depends_on": [str(node_id) for node_id in (raw.get("depends_on") or [])],
                "max_nodes": int(raw.get("max_nodes") or 0),
                "allowed_agents": [str(agent) for agent in (raw.get("allowed_agents") or [])],
                "allow_effects": bool(raw.get("allow_effects")),
            }
            for slot_id, raw in sorted((plan.get("slots") or {}).items())
            if isinstance(raw, dict)
        },
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


async def create_logical_plan(
    user_id: str,
    nodes: list[TaskNode],
    *,
    slots: list[ExpansionSlot] | None = None,
) -> dict[str, Any]:
    """Persist a complete plan while keeping the caller's Job body-free."""
    plan_id = uuid.uuid4().hex
    records = {node.id: _node_record(node) for node in nodes}
    total = sum(int(record["estimated_tokens"]) for record in records.values())
    slot_list = list(slots or [])
    node_ids = set(records)
    slot_ids = [slot.id for slot in slot_list]
    if len(slot_ids) != len(set(slot_ids)) or node_ids.intersection(slot_ids):
        raise ValueError("逻辑计划插槽 id 重复或与节点冲突")
    for slot in slot_list:
        unknown = set(slot.depends_on) - node_ids
        if unknown:
            raise ValueError(f"逻辑计划插槽依赖不存在: {', '.join(sorted(unknown))}")

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
        "slots": {slot.id: slot.model_dump(mode="json") for slot in slot_list},
    }
    plan["execution_fingerprint"] = logical_plan_execution_fingerprint(plan)
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
    return asdict(_kernel_logical_plan_progress(plan.get("nodes") or {}))


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
    selection = select_budgeted_frontier(
        records,
        list(plan.get("order") or []),
        limit=size,
        reserved=reserved,
        used=used,
        ceiling=ceiling,
    )
    for node_id in selection.node_ids:
        node_record = records[node_id]
        estimate = max(0, int(node_record.get("estimated_tokens") or 0))
        node = TaskNode.model_validate(node_record.get("node") or {})
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
        # 汇总节点不能把上一执行窗口的结果正文复制回 Job 快照。它只携带
        # 结果引用，由执行适配器在实际调用 collect_results 前按所有者解析。
        # 这样汇总既能看到指定项目，也保持滚动窗口的无正文持久化约束。
        collection_ids = (node.params or {}).get("items")
        if node.agent == "collect_results" and isinstance(collection_ids, list):
            collection_refs = []
            for collection_id in collection_ids:
                source_record = records.get(str(collection_id))
                if not isinstance(source_record, dict):
                    continue
                result_ref = source_record.get("result_ref")
                if not isinstance(result_ref, dict):
                    continue
                source = source_record.get("node") or {}
                collection_refs.append(
                    {
                        "node_id": str(collection_id),
                        "title": str(source.get("name") or collection_id)[:240],
                        "status": str(source_record.get("status") or "unknown"),
                        "result_ref": result_ref,
                    }
                )
            node.metadata["logical_collection_refs"] = collection_refs
        node_record["status"] = "materialized"
        selected.append(node)
        reserved += estimate
    budget["reserved"] = reserved
    plan["budget"] = budget
    return selected


async def commit_frontier_results(user_id: str, plan: dict[str, Any], nodes: list[TaskNode]) -> None:
    """Commit a completed execution window into the external logical plan."""
    from app.agents.orchestration.execution.lineage import ensure_node_result_ref

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


def replace_unfinished_tail(
    plan: dict[str, Any],
    nodes: list[TaskNode],
    *,
    reason: str,
    history_metadata: dict[str, Any] | None = None,
) -> None:
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
        {
            "revision": revision,
            "reason": reason[:300],
            "retired_node_ids": retired,
            "at": time.time(),
            **(history_metadata or {}),
        },
    ]
    plan["budget"] = {
        **(plan.get("budget") or {}),
        "reserved": 0,
        "estimated_total": sum(int(record.get("estimated_tokens") or 0) for record in new_records.values()),
    }
    # 替代尾部改变了尚未执行的节点定义。必须在保存前重封执行指纹，后续
    # Temporal 前沿 Activity 才能识别这是经过受控重规划得到的新计划。
    plan["execution_fingerprint"] = logical_plan_execution_fingerprint(plan)
