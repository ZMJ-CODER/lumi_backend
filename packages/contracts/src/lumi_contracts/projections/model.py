"""模型投影：把执行结果裁剪成"预算内、已脱敏、可直接进提示词"的文本/结构。

铁律（与方案第四节一致）：

* **完整正文只保存在 payload 或 artifact 中**，模型只拿到预算内的投影；
* 工作区读取结果：路径、页码/行号等**相对定位**可以给模型；绝对路径、宿主目录、
  凭据形态一律剥离；
* 投影自身带预算，不依赖下游通用截断（下游先截 JSON 再渲染会给出半截结构）。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts.projections.base import ProjectionKind, _SafeProjection

# 模型上下文预算（字符）。可按调用方覆盖。
DEFAULT_MODEL_BUDGET = 4000
# 单条命中/片段的上下文预算。
DEFAULT_ITEM_BUDGET = 320

# 不应进入模型上下文的字段名（精确匹配或后缀匹配）。
_BLOCKED_SUFFIXES = ("_path", "path", "dir", "directory", "cwd", "secret", "token", "password")
# 允许进入模型的工作区相对路径字段（白名单优先于后缀规则）。
_ALLOWED_PATH_FIELDS = frozenset({
    "path", "search_path", "relative_path", "source", "location", "file", "filename", "name",
})


def _blocked(key: str) -> bool:
    lowered = str(key or "").casefold()
    if lowered in _ALLOWED_PATH_FIELDS:
        return False
    return any(lowered == suffix or lowered.endswith(suffix) for suffix in _BLOCKED_SUFFIXES)


def _scrub(value: Any, *, depth: int = 0) -> Any:
    """递归剥离敏感字段；只做字段名级处理，不做内容改写。"""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {
            str(key): _scrub(item, depth=depth + 1)
            for key, item in value.items()
            if not _blocked(str(key))
        }
    if isinstance(value, list):
        return [_scrub(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # 未知对象（dataclass / 第三方类型）：保守序列化。
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _scrub(method(), depth=depth + 1)
            except Exception:  # noqa: BLE001
                break
    return str(value)[:500]


class ModelProjection(_SafeProjection):
    """默认模型投影：脱敏 → 有界序列化。"""

    kind = ProjectionKind.MODEL

    def __init__(self, *, budget: int = DEFAULT_MODEL_BUDGET) -> None:
        self.budget = max(200, int(budget))

    def _project(self, result: Any) -> dict[str, Any]:
        status = str(getattr(result, "status", "") or "")
        error = getattr(result, "error", None)
        tool_name = str(getattr(result, "tool_name", "") or "")
        payload = getattr(result, "payload", None)
        if payload is None and isinstance(result, dict):
            payload = result.get("payload", result.get("data"))
            status = status or str(result.get("status") or "")
            tool_name = tool_name or str(result.get("tool") or "")

        if error is not None:
            data = error.model_dump(mode="json") if hasattr(error, "model_dump") else dict(error)
            return {
                "kind": self.kind.value,
                "tool": tool_name,
                "status": status,
                "error": data,
                "has_more": False,
                "cursor": None,
            }

        scrubbed = _scrub(payload)
        has_more, cursor = _paging_hint(payload)
        text, truncated = _render(scrubbed, budget=self.budget)
        return {
            "kind": self.kind.value,
            "tool": tool_name,
            "status": status,
            "text": text,
            "truncated": truncated,
            "has_more": has_more,
            "cursor": cursor,
        }


def _paging_hint(payload: Any) -> tuple[bool, str | None]:
    """从 payload 读分页事实：扁平结构或统一信封（``data`` 嵌套）都支持。"""
    for source in (payload, getattr(payload, "meta", None)):
        if source is None:
            continue
        has_more = getattr(source, "has_more", None)
        cursor = getattr(source, "cursor", None)
        if has_more is None and isinstance(source, dict):
            has_more = source.get("has_more")
            cursor = source.get("cursor")
        if has_more is not None:
            return bool(has_more), (str(cursor) if cursor else None)
        if isinstance(source, dict):
            nested = source.get("data")
            if isinstance(nested, dict) and nested.get("has_more") is not None:
                return bool(nested["has_more"]), (str(nested.get("cursor")) if nested.get("cursor") else None)
    return False, None


def _render(value: Any, *, budget: int) -> tuple[str, bool]:
    """把（已脱敏的）payload 渲染成模型可读文本；返回 (text, truncated)。

    兼容两种 payload 形态：
    * 扁平结构（``{"sections": [...]}``）；
    * 统一信封（``{"summary": ..., "data": {"sections": [...]}, "has_more": ...}``）——
      工作区 navigator / 覆盖 Agent 用的是这一种，必须识别 ``data.sections``，
      否则正文会被序列化成 JSON 埋进 meta 噪音里。
    """
    if value is None:
        return "", False
    # 类型化 payload（pydantic / 实现了 to_dict 的业务对象）：先转成 dict 再渲染，
    # 这样分段正文能被识别，而不是退化成对象的 repr。
    if not isinstance(value, (str, dict, list, int, float, bool)):
        for attr in ("model_dump", "to_dict", "dict"):
            method = getattr(value, attr, None)
            if callable(method):
                try:
                    value = method()
                    break
                except Exception:  # noqa: BLE001 - 渲染失败时退回 str()
                    break
    if isinstance(value, str):
        return (value[:budget], len(value) > budget)
    if isinstance(value, dict):
        summary = str(value.get("summary") or "")
        for source in (value, value.get("data")):
            if not isinstance(source, dict):
                continue
            sections = source.get("sections")
            if not isinstance(sections, list) or not sections:
                continue
            lines: list[str] = []
            if summary:
                lines.append(summary)
            for item in sections:
                if not isinstance(item, dict):
                    continue
                header = f"[{item.get('source') or ''} · {item.get('location') or ''}]".strip()
                lines.append(header)
                text = str(item.get("text") or "")
                remaining = budget - sum(len(line) for line in lines)
                if remaining <= 0:
                    return "\n".join(lines)[:budget], True
                lines.append(text[:remaining])
            joined = "\n".join(lines)
            return joined[:budget], len(joined) > budget
        import json

        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            encoded = str(value)
        return encoded[:budget], len(encoded) > budget
    text = str(value)
    return text[:budget], len(text) > budget


__all__ = [
    "DEFAULT_ITEM_BUDGET",
    "DEFAULT_MODEL_BUDGET",
    "ModelProjection",
]
