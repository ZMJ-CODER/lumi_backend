"""所有执行后端共用的纯 DAG 不变量约束。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol


class DagNode(Protocol):
    id: str
    depends_on: list[str]


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    """Pure result of one dependency-driven scheduling pass."""

    ready_ids: tuple[str, ...]
    skip_ids: tuple[str, ...]


_FAILED_DEPENDENCY_STATUSES = frozenset({"failed", "skipped", "cancelled", "interrupted", "escalated"})


class DagValidationError(ValueError):
    """Raised when a graph violates a structural execution invariant."""


def validate_dag(nodes: list[DagNode]) -> None:
    """Validate unique IDs, resolvable dependencies and acyclic topology."""
    ids = {node.id for node in nodes}
    if len(ids) != len(nodes):
        raise DagValidationError("任务节点 id 重复")
    for node in nodes:
        missing = [dependency for dependency in node.depends_on if dependency not in ids]
        if missing:
            raise DagValidationError(f"节点 {node.id} 依赖不存在: {missing}")

    indegree = {node.id: 0 for node in nodes}
    children: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            children[dependency].append(node.id)
            indegree[node.id] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for child_id in children[node_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
    if visited != len(nodes):
        raise DagValidationError("任务依赖存在环，无法执行")


def decide_next_nodes(
    nodes_by_id: dict[str, Any],
    *,
    pending_ids: Iterable[str],
    completed_ids: set[str],
    settled_ids: set[str],
    waiting_resource_ids: set[str] | frozenset[str] = frozenset(),
) -> SchedulingDecision:
    """Select dependency-ready nodes without knowing workers or persistence.

    Nodes opting into ``continue_on_dependency_failure`` unlock once their
    dependencies settle. Every other node requires completed dependencies and
    is skipped as soon as a dependency reaches a failed terminal state.
    """
    ready: list[str] = []
    skipped: list[str] = []
    for node_id in sorted(pending_ids):
        node = nodes_by_id[node_id]
        metadata = getattr(node, "metadata", {}) or {}
        dependencies = tuple(getattr(node, "depends_on", ()) or ())
        if not bool(metadata.get("continue_on_dependency_failure")) and any(
            _status_value(nodes_by_id[dependency]) in _FAILED_DEPENDENCY_STATUSES
            for dependency in dependencies
        ):
            skipped.append(node_id)
            continue
        if node_id in waiting_resource_ids:
            continue
        dependency_state = settled_ids if bool(metadata.get("continue_on_dependency_failure")) else completed_ids
        if all(dependency in dependency_state for dependency in dependencies):
            ready.append(node_id)
    return SchedulingDecision(ready_ids=tuple(ready), skip_ids=tuple(skipped))


def _status_value(node: Any) -> str:
    status = getattr(node, "status", "")
    return str(getattr(status, "value", status))
