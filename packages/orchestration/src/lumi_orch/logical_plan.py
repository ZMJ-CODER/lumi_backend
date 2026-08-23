"""Pure rolling-plan progress and bounded frontier selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class LogicalPlanProgress:
    total: int
    completed: int
    failed: int
    cancelled: int
    pending: int


@dataclass(frozen=True, slots=True)
class FrontierSelection:
    node_ids: tuple[str, ...]
    reserved_increment: int


def logical_plan_progress(records: Mapping[str, Any]) -> LogicalPlanProgress:
    statuses = [
        str(record.get("status") or "pending")
        for record in records.values()
        if isinstance(record, Mapping)
    ]
    return LogicalPlanProgress(
        total=len(statuses),
        completed=statuses.count("completed"),
        failed=statuses.count("failed") + statuses.count("escalated") + statuses.count("skipped"),
        cancelled=statuses.count("cancelled"),
        pending=statuses.count("pending") + statuses.count("materialized"),
    )


def ready_node_ids(records: Mapping[str, Any], order: Sequence[str]) -> tuple[str, ...]:
    """Return pending nodes whose logical dependencies have committed."""
    ready: list[str] = []
    for node_id in order:
        record = records.get(node_id)
        if not isinstance(record, Mapping) or record.get("status") != "pending":
            continue
        raw_node = record.get("node")
        if not isinstance(raw_node, Mapping):
            continue
        dependencies = raw_node.get("depends_on") or []
        if all(
            isinstance(records.get(str(dependency)), Mapping)
            and str(records[str(dependency)].get("status") or "") == "completed"
            for dependency in dependencies
        ):
            ready.append(node_id)
    return tuple(ready)


def select_budgeted_frontier(
    records: Mapping[str, Any],
    order: Sequence[str],
    *,
    limit: int,
    reserved: int,
    used: int,
    ceiling: int,
) -> FrontierSelection:
    """Choose a stable ready prefix that fits the remaining token budget."""
    selected: list[str] = []
    increment = 0
    for node_id in ready_node_ids(records, order):
        if len(selected) >= max(1, int(limit)):
            break
        record = records[node_id]
        estimate = max(0, int(record.get("estimated_tokens") or 0))
        if int(used) + int(reserved) + increment + estimate > int(ceiling):
            break
        selected.append(node_id)
        increment += estimate
    return FrontierSelection(node_ids=tuple(selected), reserved_increment=increment)
