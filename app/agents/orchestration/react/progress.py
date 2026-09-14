"""ReAct 的**进度投影**：把过程事件发给调用方、把工具结果收进本轮记录。

职责边界很窄：
只做"往外交互"，不含任何决策——决策在 runner / 各节点里。
"""

from __future__ import annotations

from typing import Any

from app.agents.skills.base import SkillResult


class ProgressMixin:
    """进度回调与结果收集（混入 ``OfficeReactRunner``；不定义 ``__init__``）。"""

    #: 由 runner 的 ``__init__`` 赋值：进度回调（可为 None）。
    on_progress: Any = None
    #: 本轮收集到的工具结果与引用/记录。
    citations: list[dict]
    records: list[dict]

    def _emit(self, value: str | dict) -> None:
        """把一条进度事件交给调用方（没有回调时什么也不做）。"""
        if self.on_progress:
            self.on_progress(value)

    async def _on_result(self, result: SkillResult) -> None:
        """工具执行回调：收下结果，并累加它带来的引用。"""
        self._results.append(result)
        citations = result.metadata.get("citations") if isinstance(result.metadata, dict) else None
        if isinstance(citations, list):
            self.citations.extend(citations)

    async def before_tool_node(self, state: dict) -> dict:
        """``before_tool`` 节点：工具开始执行前发一条 step=running 事件。"""
        call = state["messages"][-1].tool_calls[0]
        name = str(call.get("name") or "执行工具")
        call_id = str(call.get("id") or f"react-{len(self.records) + 1}")
        self._emit({"type": "step", "id": call_id, "title": name, "status": "running", "tool": name})
        return {}


__all__ = ["ProgressMixin"]
