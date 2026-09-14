"""ReAct 的**状态机装配**：节点、边与路由条件。

这里只描述"循环长什么样"：

```text
START → agent ──(有 tool_calls)→ before_tool → tools → after_tool ──(轮数未完)→ agent
             └──(无 tool_calls)→ END                                └──(轮数用完)→ finish → END
```

节点实现（需要模型对象与逐轮状态）仍在 runner 的 ``run()`` 里，
本模块负责把它们连起来，并让路由规则可以被单独读懂与断言。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from app.agents.orchestration.react.state import ReactState


def route_agent(state: ReactState) -> str:
    """模型是否要调用工具：要 → 进 execute；不要 → 结束。"""
    message = state["messages"][-1]
    return "before_tool" if isinstance(message, AIMessage) and message.tool_calls else "end"


def build_graph(
    *,
    agent: Callable[[ReactState], Any],
    before_tool: Callable[[ReactState], Any],
    execute_tool: Callable[[ReactState], Any],
    after_tool: Callable[[ReactState], Any],
    finish: Callable[[ReactState], Any],
    max_rounds: int,
):
    """装配并编译 ReAct 图（节点语义见各调用方）。"""

    def route_tool(state: ReactState) -> str:
        return "finish" if int(state.get("rounds") or 0) >= max_rounds else "agent"

    graph = StateGraph(ReactState)
    graph.add_node("agent", agent)
    graph.add_node("before_tool", before_tool)
    graph.add_node("tools", execute_tool)
    graph.add_node("after_tool", after_tool)
    graph.add_node("finish", finish)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_agent, {"before_tool": "before_tool", "end": END})
    graph.add_edge("before_tool", "tools")
    graph.add_edge("tools", "after_tool")
    graph.add_conditional_edges("after_tool", route_tool, {"agent": "agent", "finish": "finish"})
    graph.add_edge("finish", END)
    return graph.compile()


__all__ = ["build_graph", "route_agent"]
