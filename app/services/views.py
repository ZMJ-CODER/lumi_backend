"""声明式视图（``view_updated`` 事件）的服务端白名单生产者。

第一阶段只允许**声明式**视图：后端给 ``view_type`` + 数据 + schema 版本，前端用官方
组件渲染；不开放任意 iframe / HTML / JavaScript（与 ``ViewContribution`` 契约一致）。

视图类型白名单（`lumi_contracts.plugins.view_contribution.VIEW_TYPES`）：
``table`` / ``chart`` / ``diff`` / ``timeline`` / ``file_preview`` / ``form``。

本期真正有数据源的生产者（全部复用既有安全数据，不新增采集点）：

* ``file_preview`` —— 文本类产物（``preview_generated_output`` 已给出安全文本）；
* ``table`` —— csv/tsv/xlsx 产物（同一预览器给出的有界行列）；
* ``timeline`` —— 任务步骤（``JobRunView.steps``：标题/状态/耗时，无正文）。

**不产出**的视图：需要完整 Diff 正文的 ``diff``（服务端只保留统计与引用，
完整 Diff 走受权限保护的接口按需获取）、以及需要插件数据的 ``chart`` / ``form``
（等 View Plugin 阶段接入）。

数据一律有界：单视图数据上限 ``VIEW_DATA_MAX_BYTES``，超限只留 ``data_ref``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from lumi_contracts.plugins.view_contribution import VIEW_DATA_MAX_BYTES

#: 视图 id 前缀（稳定、可去重：同一 artifact/任务只产生一个视图）。
ARTIFACT_VIEW_PREFIX = "view:artifact:"
TIMELINE_VIEW_ID = "view:timeline"

#: 预览类型 → 视图类型（只映射白名单内的声明式组件）。
_PREVIEW_TO_VIEW: dict[str, str] = {
    "table": "table",
    "text": "file_preview",
}


def _bounded(data: Any) -> tuple[dict[str, Any], str]:
    """数据超限时只保留引用（返回 ``(data, data_ref)``）。"""
    import json

    try:
        encoded = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}, ""
    if len(encoded.encode("utf-8")) > VIEW_DATA_MAX_BYTES:
        return {}, "ref:view-data-too-large"
    return data, ""


def artifact_view(user_id: str, artifact: dict[str, Any]) -> dict[str, Any] | None:
    """产物 → 声明式视图（``file_preview`` / ``table``）；不可预览时返回 ``None``。

    数据来自既有 ``office_docs.preview_generated_output``（已做安全文本化与截断），
    这里只做视图类型映射与体积上限，不再解析文件。
    """
    artifact_id = str(artifact.get("artifact_id") or "")
    filename = str(artifact.get("filename") or "")
    if not artifact_id or not filename:
        return None
    from app.services import artifacts as artifact_service
    from app.services.office_docs import preview_generated_output

    path: Path | None = artifact_service.artifact_path(user_id, artifact_id)
    if path is None:
        return None
    try:
        preview = preview_generated_output(path)
    except Exception as exc:  # noqa: BLE001 - 预览失败不影响下载能力
        logger.debug("[view] 产物预览失败（降级为仅下载）: {} ({})", filename, str(exc)[:120])
        return None
    preview_type = str(preview.get("preview_type") or "")
    view_type = _PREVIEW_TO_VIEW.get(preview_type)
    if view_type is None:
        # 不提供内嵌预览的格式：前端显示"暂不支持此展示类型，可下载产物"。
        return None
    if view_type == "table":
        data = {"rows": preview.get("rows") or [], "sheets": preview.get("sheets") or []}
    else:
        data = {"text": str(preview.get("text") or "")[:60_000]}
    if preview.get("truncated"):
        data["truncated"] = True
    bounded, data_ref = _bounded(data)
    return {
        "view_id": f"{ARTIFACT_VIEW_PREFIX}{artifact_id}",
        "view_type": view_type,
        "plugin_id": "lumi.core",
        "plugin_version": "1",
        "action": "upsert",
        "schema_version": 1,
        "title": filename[:120],
        "data": bounded,
        "data_ref": data_ref or artifact_id,
        "artifact_id": artifact_id,
    }


def timeline_view(steps: list[Any], *, job_id: str = "") -> dict[str, Any] | None:
    """任务步骤 → ``timeline`` 视图（只给标题/状态/耗时，无正文）。

    数据形状以**前端契约**为准：``data.items[]``（``services/viewContracts.js`` 里
    ``timeline`` 的 shape 校验要求 ``items``）；``steps`` 作为同义键保留，
    避免旧消费方读不到。
    """
    rows: list[dict[str, Any]] = []
    for step in steps or []:
        if isinstance(step, dict):
            step_id = str(step.get("id") or step.get("step_id") or "")
            title = str(step.get("title") or step.get("name") or "")
            status = str(step.get("status") or step.get("runtime_status") or "")
            duration = step.get("duration_ms")
        else:
            step_id = str(getattr(step, "id", "") or "")
            title = str(getattr(step, "title", "") or "")
            status = str(getattr(step, "status", "") or "")
            duration = getattr(step, "duration_ms", None)
        if not step_id:
            continue
        rows.append({
            "step_id": step_id,
            "title": title[:120],
            "status": status[:40],
            "duration_ms": int(duration or 0),
        })
    if not rows:
        return None
    items = rows[:50]
    bounded, data_ref = _bounded({"items": items, "steps": items, "job_id": str(job_id or "")})
    return {
        "view_id": TIMELINE_VIEW_ID,
        "view_type": "timeline",
        "plugin_id": "lumi.core",
        "plugin_version": "1",
        "action": "upsert",
        "schema_version": 1,
        "title": "执行步骤",
        "data": bounded,
        "data_ref": data_ref,
    }


def snapshot_contributions(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """视图 → ``run_view.view_contributions``（前端既有渲染入口的形状）。

    为什么单独投影：前端 ``ViewContainer`` 用 ``item.action || item.view_type``
    判断类型，而协议里的 ``action`` 是"更新动作"（``upsert``）。两者同名不同义，
    所以**快照投影里去掉 ``action``**，让前端回退到 ``view_type``；事件载荷
    仍按协议保留 ``action``（消费者内部会把 ``view_type`` 映射成自己的 action）。
    """
    out: list[dict[str, Any]] = []
    for view in views or []:
        if not isinstance(view, dict):
            continue
        item = {
            key: view[key]
            for key in ("view_id", "view_type", "plugin_id", "plugin_version",
                        "schema_version", "title", "data", "data_ref")
            if key in view
        }
        if not str(item.get("view_type") or ""):
            # 非白名单类型：快照里也不下发数据（与事件侧一致，前端走降级提示）
            item["data"] = {}
        out.append(item)
    return out[:20]


def views_for_job(user_id: str, job: Any, *, artifacts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """任务 → 声明式视图清单（产物预览 + 步骤时间线）。"""
    views: list[dict[str, Any]] = []
    for artifact in artifacts or []:
        view = artifact_view(user_id, artifact)
        if view is not None:
            views.append(view)
    steps = [getattr(node, "id", "") and {
        "id": getattr(node, "id", ""),
        "title": getattr(node, "name", "") or "",
        "status": getattr(getattr(node, "status", None), "value", getattr(node, "status", "")),
        "duration_ms": (
            int((getattr(node, "completed_at", 0) or 0) - (getattr(node, "started_at", 0) or 0)) * 1000
            if getattr(node, "started_at", None) is not None and getattr(node, "completed_at", None) is not None
            else 0
        ),
    } for node in (getattr(job, "nodes", []) or [])]
    timeline = timeline_view([step for step in steps if step], job_id=str(getattr(job, "job_id", "") or ""))
    if timeline is not None:
        views.append(timeline)
    return views[:20]


__all__ = [
    "ARTIFACT_VIEW_PREFIX",
    "TIMELINE_VIEW_ID",
    "artifact_view",
    "snapshot_contributions",
    "timeline_view",
    "views_for_job",
]
