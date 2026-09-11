"""四类投影的生产出口（C 项）：任意工具结果 → model / ui / audit / storage。

方案第四条：投影必须独立于业务结果类。业务代码只提供 payload，展示策略由
``ProjectionRegistry`` 决定。本模块是**唯一出口**：

* ``result_model()``：模型投影（模型可见文本，已由 tool_output_pipeline 使用）；
* ``result_ui()``：前端投影（状态、摘要、条目、分页游标、来源、错误）；
* ``result_audit()``：审计投影（谁调了什么/结果如何/耗时/错误码，无正文）；
* ``result_storage()``：持久化投影（可恢复的最小快照，白名单，无正文）。

约定：投影失败**不得**影响结果交付，任何异常都收敛为空投影（``{}``）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _execution_result(value: Any, *, tool_name: str = "") -> Any | None:
    """任意遗留结果 → ``ExecutionResult``（裸字典只在适配器内部解析）。"""
    try:
        from app.contracts import (
            is_workspace_envelope,
            to_execution_result,
            to_workspace_result,
        )

        if is_workspace_envelope(value):
            # 工作区读取结果走类型化适配，投影层才能按 payload 类型挑专用投影。
            return to_workspace_result(value, tool_name=tool_name or "workspace_navigator")
        return to_execution_result(value, tool_name=tool_name)
    except Exception as exc:  # noqa: BLE001 - 契约适配失败不能丢结果
        logger.debug("结果契约适配失败，跳过投影: {}", str(exc)[:160])
        return None


def project_result(value: Any, kind: str, *, tool_name: str = "") -> dict[str, Any]:
    """按 kind 投影任意工具结果（``model`` / ``ui`` / ``audit`` / ``storage``）。"""
    result = _execution_result(value, tool_name=tool_name)
    if result is None:
        return {}
    try:
        from app.contracts import projection_registry

        return projection_registry().project(kind, result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("{} 投影失败: {}", kind, str(exc)[:160])
        return {}


def project_all_results(value: Any, *, tool_name: str = "") -> dict[str, dict[str, Any]]:
    """一次性产出四类投影（模型/前端/审计/持久化）。"""
    result = _execution_result(value, tool_name=tool_name)
    if result is None:
        return {}
    try:
        from app.contracts import projection_registry

        return projection_registry().project_all(result)
    except Exception as exc:  # noqa: BLE001
        logger.debug("全量投影失败: {}", str(exc)[:160])
        return {}


def result_model(value: Any, *, tool_name: str = "", max_chars: int = 0) -> str:
    """模型投影文本（工作区/文档读取类结果的可读分段与分页提示）。"""
    result = _execution_result(value, tool_name=tool_name)
    if result is None:
        return ""
    try:
        from app.contracts import projection_registry

        registry = projection_registry(model_budget=max_chars) if max_chars else projection_registry()
        return str(registry.project("model", result).get("text") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("模型投影失败: {}", str(exc)[:160])
        return ""


def result_ui(value: Any, *, tool_name: str = "") -> dict[str, Any]:
    """前端投影：状态 + 摘要 + 条目/匹配/分段 + 分页游标 + 错误。"""
    return project_result(value, "ui", tool_name=tool_name)


def result_audit(value: Any, *, tool_name: str = "") -> dict[str, Any]:
    """审计投影：执行记录（不含业务正文）。"""
    return project_result(value, "audit", tool_name=tool_name)


def result_storage(value: Any, *, tool_name: str = "") -> dict[str, Any]:
    """持久化投影：可恢复的最小快照（白名单，不落正文）。"""
    return project_result(value, "storage", tool_name=tool_name)


__all__ = [
    "project_all_results",
    "project_result",
    "result_audit",
    "result_model",
    "result_storage",
    "result_ui",
]
