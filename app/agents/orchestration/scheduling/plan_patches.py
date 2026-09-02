"""向持久化逻辑计划应用受限且幂等的计划补丁。"""

from __future__ import annotations

import time
import hashlib
import json
from typing import Any

from lumi_orch import ExpansionSlot, PlanPatch, PlanPatchConflict, validate_dag

from app.agents.orchestration.logical_plan import _node_record, logical_plan_execution_fingerprint
from app.agents.orchestration.models import TaskNode


def initialize_slots(plan: dict[str, Any], slots: list[ExpansionSlot]) -> None:
    """Attach skeleton slots before the plan is first submitted."""
    if plan.get("slots"):
        raise PlanPatchConflict("计划已包含插槽，拒绝覆盖")
    _validate_slot_ids(slots, set(plan.get("nodes") or {}))
    plan["slots"] = {slot.id: slot.model_dump(mode="json") for slot in slots}
    plan["execution_fingerprint"] = logical_plan_execution_fingerprint(plan)


def apply_plan_patch(plan: dict[str, Any], patch: PlanPatch) -> bool:
    """Atomically attach a patch, returning ``False`` for an exact replay."""
    history = list(plan.get("history") or [])
    for entry in history:
        if not isinstance(entry, dict) or entry.get("patch_id") != patch.patch_id:
            continue
        recorded = str(entry.get("patch_fingerprint") or "")
        if recorded and recorded != patch_fingerprint(patch):
            raise PlanPatchConflict("patch_id 已被不同内容的补丁使用")
        return False
    if int(plan.get("revision") or 1) != patch.base_revision:
        raise PlanPatchConflict("计划版本已变化，拒绝挂载过期补丁")

    slots = plan.setdefault("slots", {})
    raw_slot = slots.get(patch.slot_id)
    if not isinstance(raw_slot, dict):
        raise PlanPatchConflict("扩图插槽不存在")
    slot = ExpansionSlot.model_validate(raw_slot)
    if slot.status != "pending":
        raise PlanPatchConflict("扩图插槽已被解析")
    _validate_patch(plan, slot, patch)

    records = plan.setdefault("nodes", {})
    for spec in patch.nodes:
        records[spec.id] = _node_record(TaskNode.model_validate(spec.model_dump(mode="json")))
        plan.setdefault("order", []).append(spec.id)
    for child in patch.slots:
        slots[child.id] = child.model_dump(mode="json")
    slots[slot.id] = slot.model_copy(update={"status": "expanded"}).model_dump(mode="json")
    plan["revision"] = int(plan.get("revision") or 1) + 1
    plan["history"] = [
        *history[-19:],
        {
            "event": "plan_patch_applied",
            "patch_id": patch.patch_id,
            "slot_id": patch.slot_id,
            "source": patch.source,
            "patch_fingerprint": patch_fingerprint(patch),
            "revision": plan["revision"],
            "node_ids": [node.id for node in patch.nodes],
            "child_slot_ids": [child.id for child in patch.slots],
            "at": time.time(),
        },
    ]
    plan["execution_fingerprint"] = logical_plan_execution_fingerprint(plan)
    return True


def patch_fingerprint(patch: PlanPatch) -> str:
    """为幂等重放绑定补丁正文，不把同一 id 误认作另一份请求。"""
    raw = json.dumps(
        patch.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ready_slots(plan: dict[str, Any]) -> list[ExpansionSlot]:
    """Return unresolved slots whose upstream node results are committed."""
    records = plan.get("nodes") or {}
    return [
        slot
        for raw in (plan.get("slots") or {}).values()
        if isinstance(raw, dict)
        for slot in [ExpansionSlot.model_validate(raw)]
        if slot.status == "pending"
        and all(str((records.get(node_id) or {}).get("status") or "") == "completed" for node_id in slot.depends_on)
    ]


def _validate_patch(plan: dict[str, Any], slot: ExpansionSlot, patch: PlanPatch) -> None:
    if not patch.nodes and not patch.slots:
        raise PlanPatchConflict("扩图补丁至少需要一个节点或子插槽")
    if len(patch.nodes) > slot.max_nodes:
        raise PlanPatchConflict("扩图节点数超过插槽上限")
    existing_ids = set(plan.get("nodes") or {})
    added_ids = {node.id for node in patch.nodes}
    if existing_ids & added_ids:
        raise PlanPatchConflict("扩图节点 id 与现有计划冲突")
    _validate_slot_ids(list(patch.slots), existing_ids | added_ids | set(plan.get("slots") or {}))
    for node in patch.nodes:
        if slot.allowed_agents and node.agent not in slot.allowed_agents:
            raise PlanPatchConflict(f"节点 agent {node.agent} 不在插槽白名单")
        effectful = bool(node.idempotency_key or node.resource_claims)
        if not slot.allow_effects and (node.approval or effectful):
            raise PlanPatchConflict("该插槽不允许副作用节点")
        if effectful and not node.approval:
            raise PlanPatchConflict("副作用扩图节点必须预先声明审批")
    allowed_dependencies = existing_ids | added_ids
    for child in patch.slots:
        unknown = set(child.depends_on) - allowed_dependencies
        if unknown:
            raise PlanPatchConflict(f"子插槽依赖不存在: {', '.join(sorted(unknown))}")
    all_nodes = [
        TaskNode.model_validate((record or {}).get("node") or {})
        for record in (plan.get("nodes") or {}).values()
    ] + [TaskNode.model_validate(node.model_dump(mode="json")) for node in patch.nodes]
    try:
        validate_dag(all_nodes)
    except ValueError as exc:
        raise PlanPatchConflict(str(exc)) from exc


def _validate_slot_ids(slots: list[ExpansionSlot], reserved: set[str]) -> None:
    ids = [slot.id for slot in slots]
    if len(ids) != len(set(ids)) or reserved & set(ids):
        raise PlanPatchConflict("扩图插槽 id 重复或冲突")
