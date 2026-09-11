"""阶段 7（后端）：声明式视图投影 —— 把能力结果转成前端可直接渲染的视图。

第一版**只允许声明式视图**（``ViewContribution`` 契约里没有"代码"字段）：

    table / chart / diff / timeline / file_preview / form

三条硬约束：

1. **白名单类型**：未知 ``view_type`` 由契约层拒绝（自定义交互必须走隔离容器）；
2. **数据预算**：超过 ``VIEW_DATA_MAX_BYTES`` 的投影只保留前若干行 + ``truncated`` 标记，
   绝不把整篇正文塞进"视图"；
3. **不泄露本地细节**：``file_preview`` 只给**文件名与统计**，不给绝对路径或正文
   （正文走 artifact/受限读取通道，视图只负责"呈现"）。

投影是**纯函数**：输入能力 payload，输出 ``ViewContribution``；不查询、不落库。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    VIEW_DATA_MAX_BYTES,
    VIEW_TYPES,
    ViewContribution,
)

#: 各视图类型的行数上限（预算之内的软上限，避免"合法但不可读"的超大表格）。
MAX_TABLE_ROWS = 200
MAX_CHART_POINTS = 500
MAX_TIMELINE_ITEMS = 200
MAX_DIFF_LINES = 400


def _size_of(data: dict[str, Any]) -> int:
    try:
        return len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _bounded(data: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """把数据裁到预算内；裁过就标记 ``truncated``（前端据此提示"仅显示部分"）。"""
    if _size_of(data) <= VIEW_DATA_MAX_BYTES:
        return data
    trimmed = dict(data)
    items = trimmed.pop("rows", None) or trimmed.pop("points", None) or trimmed.pop("items", None)
    if isinstance(items, list):
        keep = max(1, len(items) // 2)
        key = "rows" if "rows" in data else ("points" if "points" in data else "items")
        while keep > 1 and _size_of({**trimmed, key: items[:keep]}) > VIEW_DATA_MAX_BYTES:
            keep //= 2
        trimmed[key] = items[:keep]
    trimmed["truncated"] = True
    trimmed["truncated_reason"] = f"{kind} 数据超过视图预算，仅显示部分内容"
    logger.debug("[view] {} 投影超预算已截断", kind)
    return trimmed


def _as_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            rows.append({str(key): _scalar(item[key]) for key in item})
    return rows


def _scalar(value: Any) -> Any:
    """单元格只放标量（嵌套对象转字符串摘要），避免前端渲染复杂结构。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return f"[{len(value)} 项]"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)[:200]
    return str(value)


def table_view(
    rows: Any,
    *,
    title: str = "",
    source: str = "",
    sensitivity: str = "",
) -> ViewContribution:
    data: dict[str, Any] = {"rows": _as_rows(rows)[:MAX_TABLE_ROWS]}
    if len(_as_rows(rows)) > MAX_TABLE_ROWS:
        data["truncated"] = True
    return ViewContribution(
        view_type="table",
        data=_bounded(data, kind="table"),
        title=title,
        source=source,
        sensitivity=sensitivity,
    )


def chart_view(
    points: Any,
    *,
    chart_type: str = "line",
    title: str = "",
    source: str = "",
) -> ViewContribution:
    series: list[dict[str, Any]] = []
    for item in points if isinstance(points, list) else []:
        if isinstance(item, dict):
            series.append({"x": _scalar(item.get("x")), "y": _scalar(item.get("y")), "label": _scalar(item.get("label"))})
        elif isinstance(item, (int, float)):
            series.append({"x": len(series), "y": item})
    data = {"chart_type": chart_type, "points": series[:MAX_CHART_POINTS]}
    if len(series) > MAX_CHART_POINTS:
        data["truncated"] = True
    return ViewContribution(view_type="chart", data=_bounded(data, kind="chart"), title=title, source=source)


def diff_view(
    diff_text: str,
    *,
    path: str = "",
    title: str = "",
    source: str = "",
) -> ViewContribution:
    lines: list[dict[str, Any]] = []
    for raw in str(diff_text or "").splitlines():
        kind = "context"
        if raw.startswith("+") and not raw.startswith("+++"):
            kind = "add"
        elif raw.startswith("-") and not raw.startswith("---"):
            kind = "remove"
        elif raw.startswith("@@"):
            kind = "hunk"
        lines.append({"kind": kind, "text": raw[:400]})
    data: dict[str, Any] = {
        # 只给文件名（不暴露目录结构）
        "path": str(path or "").replace("\\", "/").rsplit("/", 1)[-1][:200],
        "lines": lines[:MAX_DIFF_LINES],
        "added": sum(1 for item in lines if item["kind"] == "add"),
        "removed": sum(1 for item in lines if item["kind"] == "remove"),
    }
    if len(lines) > MAX_DIFF_LINES:
        data["truncated"] = True
    return ViewContribution(view_type="diff", data=_bounded(data, kind="diff"), title=title, source=source)


def timeline_view(
    items: Any,
    *,
    title: str = "",
    source: str = "",
) -> ViewContribution:
    rows: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            rows.append(
                {
                    "at": str(item.get("at") or item.get("time") or "")[:64],
                    "title": str(item.get("title") or "")[:200],
                    "status": str(item.get("status") or "")[:40],
                }
            )
    data = {"items": rows[:MAX_TIMELINE_ITEMS]}
    if len(rows) > MAX_TIMELINE_ITEMS:
        data["truncated"] = True
    return ViewContribution(view_type="timeline", data=_bounded(data, kind="timeline"), title=title, source=source)


def file_preview_view(
    *,
    name: str,
    size: int = 0,
    media_type: str = "",
    summary: str = "",
    title: str = "",
    source: str = "",
) -> ViewContribution:
    """文件预览：**只给名称/大小/类型/摘要**，不给路径也不给正文。"""
    data = {
        "name": str(name or "")[:200],
        "size": int(size or 0),
        "media_type": str(media_type or "")[:120],
        "summary": str(summary or "")[:400],
    }
    return ViewContribution(view_type="file_preview", data=data, title=title, source=source)


def form_view(
    fields: Any,
    *,
    title: str = "",
    source: str = "",
    submit_label: str = "提交",
) -> ViewContribution:
    rows: list[dict[str, Any]] = []
    for item in fields if isinstance(fields, list) else []:
        if isinstance(item, dict):
            rows.append(
                {
                    "name": str(item.get("name") or "")[:120],
                    "label": str(item.get("label") or "")[:200],
                    "type": str(item.get("type") or "text")[:40],
                    "required": bool(item.get("required")),
                }
            )
    return ViewContribution(
        view_type="form",
        data={"fields": rows, "submit_label": str(submit_label)[:60]},
        title=title,
        source=source,
    )


def file_preview_from_artifact(name: str, *, size: int = 0, media_type: str = "", summary: str = "", source: str = "") -> ViewContribution:
    """artifact 引用 → 文件预览视图（产物通道的默认呈现）。"""
    return file_preview_view(
        name=name, size=size, media_type=media_type, summary=summary, source=source
    )


def views_for_payload(payload: Any, *, source: str = "", capability: str = "") -> list[ViewContribution]:
    """按既有 payload 形状挑选视图（**不猜业务**，只认已经存在的安全字段）。

    支持的形状：``rows`` / ``entries`` / ``matches`` → table；``activities``/``events``
    → timeline；``artifacts`` → file_preview；``diff`` → diff。
    其它形状返回空列表（由调用方决定用哪种视图，不硬猜）。
    """
    if not isinstance(payload, dict):
        return []
    origin = source or capability
    views: list[ViewContribution] = []
    for key in ("rows", "entries", "matches"):
        if isinstance(payload.get(key), list) and payload[key]:
            views.append(table_view(payload[key], source=origin, title=key))
            break
    for key in ("activities", "events"):
        if isinstance(payload.get(key), list) and payload[key]:
            views.append(timeline_view(payload[key], source=origin, title=key))
            break
    if isinstance(payload.get("diff"), str) and payload["diff"]:
        views.append(diff_view(payload["diff"], path=str(payload.get("path") or ""), source=origin))
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts[:8]:
            if isinstance(item, dict) and item.get("name"):
                views.append(
                    file_preview_from_artifact(
                        str(item.get("name")),
                        size=int(item.get("size") or 0),
                        media_type=str(item.get("media_type") or ""),
                        source=origin,
                    )
                )
    return views


def supported_view_types() -> tuple[str, ...]:
    return tuple(sorted(VIEW_TYPES))


__all__ = [
    "MAX_CHART_POINTS",
    "MAX_DIFF_LINES",
    "MAX_TABLE_ROWS",
    "MAX_TIMELINE_ITEMS",
    "chart_view",
    "diff_view",
    "file_preview_from_artifact",
    "file_preview_view",
    "form_view",
    "supported_view_types",
    "table_view",
    "timeline_view",
    "views_for_payload",
]
