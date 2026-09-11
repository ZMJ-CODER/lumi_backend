"""UI 投影：前端展示所需的字段、状态与"可继续"信息。

前端拿到的不是原始 payload，而是**已确定形状**的展示契约：状态、摘要、条目列表、
分页游标、来源、错误提示。新增字段必须保持向后兼容（前端对未知字段容忍）。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts.projections.base import ProjectionKind, _SafeProjection, payload_mapping
from lumi_contracts.projections.model import _paging_hint

# UI 只展示这些字段（按 payload 语义挑选），避免把内部结构整包下发。
_ENTRY_FIELDS = ("name", "path", "kind", "size", "modified_at", "ext", "ignored")
_MATCH_FIELDS = ("path", "location", "line", "page", "sheet", "format", "sensitive")
_SECTION_FIELDS = ("source", "location", "title")


class UiProjection(_SafeProjection):
    """默认 UI 投影：状态 + 摘要 + 条目 + 分页 + 来源。"""

    kind = ProjectionKind.UI

    def __init__(self, *, preview_chars: int = 400) -> None:
        self.preview_chars = max(0, int(preview_chars))

    def _project(self, result: Any) -> dict[str, Any]:
        status = str(getattr(result, "status", "") or "")
        tool_name = str(getattr(result, "tool_name", "") or "")
        call_id = str(getattr(result, "call_id", "") or "")
        error = getattr(result, "error", None)
        payload = getattr(result, "payload", None)
        if payload is None and isinstance(result, dict):
            payload = result.get("payload", result.get("data"))
            status = status or str(result.get("status") or "")
            tool_name = tool_name or str(result.get("tool") or "")
        # 类型化 payload（pydantic / to_dict 对象）也要能投影，否则前端只会拿到空条目。
        payload = payload_mapping(payload)

        view: dict[str, Any] = {
            "kind": self.kind.value,
            "tool": tool_name,
            "call_id": call_id,
            "status": status,
            "retryable": bool(getattr(result, "retryable", False)),
        }
        if error is not None:
            data = error.model_dump(mode="json") if hasattr(error, "model_dump") else dict(error)
            view["error"] = {
                "code": data.get("code"),
                "message": data.get("message"),
                "suggested_action": data.get("suggested_action"),
            }
            return view

        has_more, cursor = _paging_hint(payload)
        view["has_more"] = has_more
        view["cursor"] = cursor
        view["summary"] = _summary_of(payload)
        view["entries"] = _pick_list(payload, ("entries",), _ENTRY_FIELDS)
        view["matches"] = _pick_list(payload, ("matches",), _MATCH_FIELDS)
        sections = _pick_list(payload, ("sections",), _SECTION_FIELDS)
        view["sections"] = sections
        if self.preview_chars and isinstance(payload, dict):
            text = str(payload.get("text") or "")
            if text:
                view["preview"] = text[: self.preview_chars]
        art = getattr(result, "artifact_refs", None) or []
        if art:
            view["artifacts"] = [
                item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else dict(item)
                for item in art
            ]
        return view


def _summary_of(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("summary") or "")[:500]
    for attr in ("summary",):
        value = getattr(payload, attr, None)
        if value:
            return str(value)[:500]
    return ""


def _pick_list(payload: Any, keys: tuple[str, ...], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """从 payload 里取列表。

    兼容两种形态：
    * 扁平 payload（``{"entries": [...]}``）；
    * 统一信封（``{"data": {"entries": [...]}}``，工作区 navigator/覆盖 Agent 用）。
    """
    if not isinstance(payload, dict):
        return []
    for source in (payload, payload.get("data"), payload.get("meta")):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if not isinstance(value, list):
                continue
            out: list[dict[str, Any]] = []
            for item in value[:200]:
                if isinstance(item, dict):
                    picked = {field: item.get(field) for field in fields if field in item}
                    out.append(picked)
                elif isinstance(item, str):
                    out.append({fields[0]: item})
            if out:
                return out
    return []


__all__ = ["UiProjection"]
