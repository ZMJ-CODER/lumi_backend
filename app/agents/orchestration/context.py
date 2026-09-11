"""节点间只传递有限、脱敏、JSON 安全的结构化结果。"""

from __future__ import annotations

import json
import re


_SENSITIVE_KEY = re.compile(
    r"(^|_)(password|passwd|secret|token|api_key|access_key|private_key|authorization|cookie)($|_)",
    re.IGNORECASE,
)
_ALLOWED_KEYS = {
    "success", "content", "output", "answer", "summary", "items", "results",
    "path", "doc_id", "project_id", "filename", "citations", "count", "status",
    "tool", "step_title", "metadata", "execution", "read_evidence", "tool_metadata",
    "display", "attempt", "method_chain", "answered_from_evidence", "read_complete",
    # 保留失败/分页事实，供下游节点决定是否诚实降级或继续读取。
    "error", "error_code", "retryable", "action", "has_more", "cursor", "notes",
    # 原始 ToolOutput 信封可能直接作为 lineage 结果持久化；不能只支持
    # AtomicStep 的 execution 包装形态，否则回放时正文会被白名单丢弃。
    "data", "content_type", "call_id", "meta",
}


def _sanitize(value, *, depth: int = 0, text_limit: int = 6000, max_depth: int = 4):
    if depth > max_depth:
        return "[已裁剪]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        limit = max(500, int(text_limit or 6000))
        return value[:limit] + ("…[已截断]" if len(value) > limit else "")
    if isinstance(value, list):
        return [_sanitize(v, depth=depth + 1, text_limit=text_limit, max_depth=max_depth) for v in value[:30]]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                continue
            if depth == 0 and name not in _ALLOWED_KEYS:
                continue
            out[name] = _sanitize(item, depth=depth + 1, text_limit=text_limit, max_depth=max_depth)
        return out
    return str(value)[:1000]


def _is_workspace_evidence(result: dict | None) -> bool:
    value = result if isinstance(result, dict) else {}
    if str(value.get("tool") or "") in {"workspace_navigator", "workspace_read", "workspace_coverage"}:
        return True
    if value.get("read_evidence"):
        return True
    metadata = value.get("tool_metadata")
    if isinstance(metadata, dict):
        coverage = metadata.get("workspace_coverage")
        if isinstance(coverage, dict) or str(metadata.get("tool") or "") in {
            "workspace_navigator", "workspace_read", "workspace_coverage",
        }:
            return True
    # 原始 ToolOutput / MCP 信封：data 下面直接带 navigator action/sections。
    data = value.get("data")
    if isinstance(data, dict):
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        if data.get("action") == "read" or isinstance(inner.get("sections"), list):
            return True
    execution = value.get("execution")
    if isinstance(execution, dict):
        data = execution.get("data")
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            if data.get("action") == "read" or isinstance(inner.get("sections"), list):
                return True
    return False


def sanitize_dependency_result(result: dict | None, max_chars: int = 12000) -> dict:
    """保留后续步骤真正需要的字段，并限制单依赖体积。"""
    workspace_evidence = _is_workspace_evidence(result)
    effective_max_chars = max(int(max_chars or 12000), 120000 if workspace_evidence else 0)
    cleaned = _sanitize(
        result or {},
        text_limit=120000 if workspace_evidence else 6000,
        # Structured MCP execution envelopes add several data/meta layers
        # before reaching sections[].text.  The legacy depth=4 guard clipped
        # those sections to "[已裁剪]", leaving direct_llm with no evidence.
        max_depth=8 if workspace_evidence else 4,
    )
    if not isinstance(cleaned, dict):
        cleaned = {"content": cleaned}
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= effective_max_chars:
        return cleaned
    for key in ("content", "output", "answer", "summary"):
        if isinstance(cleaned.get(key), str):
            cleaned[key] = cleaned[key][: max(500, effective_max_chars // 2)] + "…[已截断]"
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= effective_max_chars:
        return cleaned
    return {"summary": encoded[:effective_max_chars] + "…[依赖结果已裁剪]"}


def build_dependency_context(node, node_by_id: dict, max_total_chars: int = 24000) -> dict:
    out = {}
    used = 0
    for dep_id in node.depends_on:
        dep = node_by_id.get(dep_id)
        if dep is None:
            continue
        status = dep.status.value if hasattr(dep.status, "value") else str(dep.status)
        if status != "completed":
            continue
        cleaned = sanitize_dependency_result(dep.result)
        size = len(json.dumps(cleaned, ensure_ascii=False, default=str))
        budget = max(max_total_chars, 120000) if _is_workspace_evidence(dep.result) else max_total_chars
        if used + size > budget:
            out[dep_id] = {"summary": "[依赖结果总量达到上限，已省略]"}
            break
        out[dep_id] = cleaned
        used += size
    return out


async def build_dependency_context_from_refs(
    node,
    node_by_id: dict,
    *,
    user_id: str,
    max_total_chars: int = 24000,
) -> dict:
    """Build dependency context while resolving replay prefixes by reference.

    Ordinary nodes use their in-memory result. A forked prefix deliberately
    has no result body in the new Job snapshot, so only this execution-time
    resolver reads its sanitized body from the owner-scoped result store.
    """
    out = {}
    used = 0
    # A rolling logical plan materializes only the ready frontier.  Direct
    # dependencies are therefore external result references rather than nodes
    # in this transient execution DAG.
    external_refs = (getattr(node, "metadata", {}) or {}).get("logical_dependency_refs") or {}
    for dep_id, result_ref in external_refs.items():
        result = await _resolve_dependency_ref(user_id, result_ref)
        if result:
            cleaned = sanitize_dependency_result(result)
            size = len(json.dumps(cleaned, ensure_ascii=False, default=str))
            budget = max(max_total_chars, 120000) if _is_workspace_evidence(result) else max_total_chars
            if used + size > budget:
                out[str(dep_id)] = {"summary": "[依赖结果总量达到上限，已省略]"}
                return out
            out[str(dep_id)] = cleaned
            used += size
        else:
            out[str(dep_id)] = {
                "summary": "[前序结果引用不可用，需重新执行该前序步骤]",
                "error_code": "RESULT_REF_EXPIRED",
            }
    for dep_id in node.depends_on:
        dep = node_by_id.get(dep_id)
        if dep is None:
            continue
        status = dep.status.value if hasattr(dep.status, "value") else str(dep.status)
        if status != "completed":
            continue
        result = dep.result
        if not result:
            result = await _resolve_dependency_ref(user_id, (dep.metadata or {}).get("result_ref"))
        if not result:
            out[dep_id] = {
                "summary": "[前序结果引用不可用，需重新执行该前序步骤]",
                "error_code": "RESULT_REF_EXPIRED",
            }
            continue
        cleaned = sanitize_dependency_result(result)
        size = len(json.dumps(cleaned, ensure_ascii=False, default=str))
        budget = max(max_total_chars, 120000) if _is_workspace_evidence(result) else max_total_chars
        if used + size > budget:
            out[dep_id] = {"summary": "[依赖结果总量达到上限，已省略]"}
            break
        out[dep_id] = cleaned
        used += size
    return out


async def _resolve_dependency_ref(user_id: str, result_ref: dict | None) -> dict | None:
    from app.agents.orchestration.execution.lineage import resolve_result_ref

    return await resolve_result_ref(user_id, result_ref)
