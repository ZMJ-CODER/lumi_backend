"""受控办公 ReAct 执行器的**数据形状**（状态与结果）。

这里只有数据结构、没有行为，因此任何模块都可以引用它，而不必拉起整个 ReAct 循环。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ReactState(TypedDict, total=False):
    """LangGraph 的循环状态（消息、轮数、本轮允许的工具、是否需要澄清）。"""

    messages: Annotated[list[BaseMessage], add_messages]
    rounds: int
    allowed_tools: list[str]
    force_clarification: bool


@dataclass
class ReactRunResult:
    """一次 ReAct 运行的结果（成功/失败、正文、过程记录、引用与度量）。"""

    success: bool
    content: str = ""
    error: str | None = None
    error_code: str | None = None
    records: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    selection_traces: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


__all__ = ["ReactRunResult", "ReactState"]
