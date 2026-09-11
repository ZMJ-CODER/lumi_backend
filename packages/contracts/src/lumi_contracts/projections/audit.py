"""审计投影：完整元数据与执行记录（不含业务正文，避免日志膨胀与泄漏）。"""

from __future__ import annotations

from typing import Any

from lumi_contracts.projections.base import ProjectionKind, _SafeProjection, payload_mapping


class AuditProjection(_SafeProjection):
    """审计只需要"谁、调了什么、结果如何、花了多久、错误码"，不需要正文。"""

    kind = ProjectionKind.AUDIT

    def _project(self, result: Any) -> dict[str, Any]:
        error = getattr(result, "error", None)
        timing = getattr(result, "timing", None)
        metadata = getattr(result, "metadata", None) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        record: dict[str, Any] = {
            "kind": self.kind.value,
            "tool": str(getattr(result, "tool_name", "") or ""),
            "namespace": str(getattr(result, "namespace", "") or ""),
            "status": str(getattr(result, "status", "") or ""),
            "schema": str(getattr(result, "schema_name", "") or ""),
            "schema_version": int(getattr(result, "schema_version", 1) or 1),
            "call_id": str(getattr(result, "call_id", "") or ""),
            "request_id": str(getattr(result, "request_id", "") or ""),
            "trace_id": str(getattr(result, "trace_id", "") or ""),
            "job_id": str(getattr(result, "job_id", "") or ""),
            "node_id": str(getattr(result, "node_id", "") or ""),
            "retryable": bool(getattr(result, "retryable", False)),
            "partial": bool(getattr(result, "partial", False)),
            "sensitivity": str(getattr(result, "sensitivity", "") or ""),
            "error_code": getattr(error, "code", None) if error is not None else None,
            "duration_ms": int(getattr(timing, "duration_ms", 0) or 0),
            "artifact_count": len(getattr(result, "artifact_refs", None) or []),
            "item_count": _item_count(payload_mapping(getattr(result, "payload", None))),
            # 只保留已声明的质量提示/决策信号，不透传整包 payload。
            "quality_hints": _pick(metadata.get("quality_hints")),
            "decision_signals": _pick(metadata.get("decision_signals")),
        }
        return record


def _item_count(payload: Any) -> int:
    if isinstance(payload, dict):
        total = 0
        for key in ("entries", "matches", "sections"):
            value = payload.get(key)
            if isinstance(value, list):
                total += len(value)
        return total
    if isinstance(payload, list):
        return len(payload)
    return 0


def _pick(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): value[key] for key in list(value)[:20]}
    return {}


__all__ = ["AuditProjection"]
