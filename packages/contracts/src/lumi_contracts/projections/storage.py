"""持久化投影：可恢复的**最小快照**。

用途：任务恢复 / 逻辑计划滚动窗口 / fork 重放。要求：

* 足以恢复"下一步该做什么"（状态、游标、产物引用、错误码）；
* **不落业务正文**（正文放在 artifact 或 result_ref 里按需解析）；
* 只保留白名单字段，新增字段必须显式加入，避免把内部结构写进状态库。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts.projections.base import ProjectionKind, _SafeProjection, payload_mapping
from lumi_contracts.projections.model import _paging_hint

# 持久化白名单：只有这些键允许写入快照。
_SNAPSHOT_KEYS = frozenset({
    "summary", "format", "parser", "workspace_id", "workspace_version",
    "has_more", "cursor", "sensitive", "sensitivity", "redacted",
    "redaction_count", "partial", "truncated", "tool", "action", "path",
    "coverage", "coverage_target", "read_complete", "pages_read",
    "char_count", "sections_returned", "filtered_ignored", "include_ignored",
    "match_count", "read_count", "failed_count", "skipped_count",
})


class StorageProjection(_SafeProjection):
    """默认持久化投影：白名单 + 分页指针 + 产物引用。"""

    kind = ProjectionKind.STORAGE

    def _project(self, result: Any) -> dict[str, Any]:
        payload = getattr(result, "payload", None)
        if payload is None and isinstance(result, dict):
            payload = result.get("payload", result.get("data"))
        payload = payload_mapping(payload)
        has_more, cursor = _paging_hint(payload)
        snapshot: dict[str, Any] = {
            "kind": self.kind.value,
            "tool": str(getattr(result, "tool_name", "") or ""),
            "status": str(getattr(result, "status", "") or ""),
            "schema": str(getattr(result, "schema_name", "") or ""),
            "schema_version": int(getattr(result, "schema_version", 1) or 1),
            "call_id": str(getattr(result, "call_id", "") or ""),
            "has_more": has_more,
            "cursor": cursor,
            "error_code": getattr(getattr(result, "error", None), "code", None),
            "artifact_refs": [
                item.model_dump(mode="json", exclude_none=True)
                if hasattr(item, "model_dump") else dict(item)
                for item in (getattr(result, "artifact_refs", None) or [])
            ],
        }
        # navigator/覆盖类结果的分页与覆盖度白名单字段直接取自 payload。
        if isinstance(payload, dict):
            for key in _SNAPSHOT_KEYS:
                if key in payload and key not in {"has_more", "cursor"}:
                    snapshot[key] = payload[key]
            nested_meta = payload.get("meta")
            if isinstance(nested_meta, dict):
                for key in _SNAPSHOT_KEYS:
                    if key in nested_meta and key not in snapshot:
                        snapshot[key] = nested_meta[key]
        return snapshot


__all__ = ["StorageProjection"]
