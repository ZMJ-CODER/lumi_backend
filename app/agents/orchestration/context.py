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
    "tool", "step_title", "metadata",
}


def _sanitize(value, *, depth: int = 0):
    if depth > 4:
        return "[已裁剪]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:6000] + ("…[已截断]" if len(value) > 6000 else "")
    if isinstance(value, list):
        return [_sanitize(v, depth=depth + 1) for v in value[:30]]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                continue
            if depth == 0 and name not in _ALLOWED_KEYS:
                continue
            out[name] = _sanitize(item, depth=depth + 1)
        return out
    return str(value)[:1000]


def sanitize_dependency_result(result: dict | None, max_chars: int = 12000) -> dict:
    """保留后续步骤真正需要的字段，并限制单依赖体积。"""
    cleaned = _sanitize(result or {})
    if not isinstance(cleaned, dict):
        cleaned = {"content": cleaned}
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return cleaned
    for key in ("content", "output", "answer", "summary"):
        if isinstance(cleaned.get(key), str):
            cleaned[key] = cleaned[key][: max(500, max_chars // 2)] + "…[已截断]"
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return cleaned
    return {"summary": encoded[:max_chars] + "…[依赖结果已裁剪]"}


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
        if used + size > max_total_chars:
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
            if used + size > max_total_chars:
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
        if used + size > max_total_chars:
            out[dep_id] = {"summary": "[依赖结果总量达到上限，已省略]"}
            break
        out[dep_id] = cleaned
        used += size
    return out


async def _resolve_dependency_ref(user_id: str, result_ref: dict | None) -> dict | None:
    from app.agents.orchestration.execution_lineage import resolve_result_ref

    return await resolve_result_ref(user_id, result_ref)
