"""Pure cursor and progress semantics for rolling task manifests."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ManifestProgress:
    total: int
    completed: int
    failed: int
    cancelled: int
    cursor: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def clamp_cursor(cursor: Any, total: int) -> int:
    try:
        value = int(cursor or 0)
    except (TypeError, ValueError):
        value = 0
    return min(max(value, 0), max(0, int(total)))


def manifest_progress(items: Sequence[Mapping[str, Any]], cursor: Any) -> ManifestProgress:
    total = len(items)
    return ManifestProgress(
        total=total,
        completed=sum(1 for item in items if item.get("status") == "completed"),
        failed=sum(1 for item in items if item.get("status") == "failed"),
        cancelled=sum(1 for item in items if item.get("status") == "cancelled"),
        cursor=clamp_cursor(cursor, total),
    )


def next_manifest_batch(items: Sequence[Any], *, cursor: Any, batch_size: Any) -> list[Any]:
    """Return one bounded cursor window without advancing it."""
    start = clamp_cursor(cursor, len(items))
    try:
        size = max(1, int(batch_size or 1))
    except (TypeError, ValueError):
        size = 1
    return list(items[start:start + size])


def advance_cursor(cursor: Any, *, total: int, settled_items: int) -> int:
    """Advance only for formerly-pending items that reached a terminal outcome."""
    return clamp_cursor(clamp_cursor(cursor, total) + max(0, int(settled_items)), total)
