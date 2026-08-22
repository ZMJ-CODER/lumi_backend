"""受控工具图。

LangGraph 负责 ``model -> before_tool -> ToolNode -> after_tool -> model`` 的
消息流转。工具的授权、
审计、用户隔离和脱敏仍在 ``execute_tool_call`` 中执行；因此模型无法通过
LangChain 绕过 Lumi 的场景白名单或访问其他用户资源。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, TypedDict
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, convert_to_messages
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.langchain.models import get_chat_model
from app.agents.langchain.tools import make_skill_tool
from app.agents.skills.base import SkillResult
from app.agents.skills.executor import get_capabilities_for_scene
from app.core.agent_security import redact_server_text, wrap_untrusted_tool_output


class ChatGraphState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_rounds: int


ProgressCallback = Callable[[str | dict], None]


def _safe_tool_error(_exc: Exception) -> str:
    """不回显 Pydantic/供应商细节，仍允许模型基于错误继续完成回答。"""
    return "工具调用未执行：参数不符合当前已授权工具的要求，请修正参数、换一种方法或直接说明限制。"


class LangGraphChatRunner:
    """每次请求创建短生命周期工具图，避免跨用户或场景复用工具实例。"""

    def __init__(
        self,
        *,
        user_id: str,
        scene: str = "chat",
        conversation_id: str = "",
        user_role: str = "user",
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        llm_config: dict[str, Any] | None = None,
        max_rounds: int = 5,
        on_progress: ProgressCallback | None = None,
        chat_model: Runnable | None = None,
    ) -> None:
        self.user_id = user_id
        self.scene = scene
        self.conversation_id = conversation_id
        self.user_role = user_role
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url
        self.llm_config = llm_config
        self.max_rounds = max(1, max_rounds)
        self.on_progress = on_progress
        self.chat_model = chat_model
        self.records: list[dict] = []
        self.citations: list[dict] = []
        self._tool_results: list[SkillResult] = []

    def _emit(self, value: str | dict) -> None:
        if self.on_progress:
            self.on_progress(value)

    async def _on_tool_result(self, result: SkillResult) -> None:
        self._tool_results.append(result)
        if isinstance(result.metadata.get("citations"), list):
            self.citations.extend(result.metadata["citations"])

    async def run(self, messages: list[dict] | list[BaseMessage]) -> tuple[str, list[dict], list[dict]]:
        """执行串行工具循环，返回旧 ``run_skill_loop`` 保持的三元组契约。"""
        current_user_message = ""
        for message in reversed(messages):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "type", "")
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if role in {"user", "human"} and isinstance(content, str):
                current_user_message = content
                break
        capabilities = await get_capabilities_for_scene(self.scene, self.user_role, self.user_id)
        tools = []
        for capability in capabilities:
            tool = await make_skill_tool(
                capability.name,
                user_id=self.user_id,
                scene=self.scene,
                conversation_id=self.conversation_id,
                user_role=self.user_role,
                on_notify=self._emit,
                on_result=self._on_tool_result,
                user_message=current_user_message,
                llm_config=self.llm_config,
            )
            if tool is not None:
                tools.append(tool)
        if not tools:
            return "", [], []

        model = self.chat_model or await get_chat_model(
            scene=self.scene,
            user_id=self.user_id,
            api_key=self.api_key,
            model=self.model_name,
            base_url=self.base_url,
            llm_config=self.llm_config,
        )
        bound_model = model.bind_tools(tools)

        async def wrap_tool_result(request, execute):
            """将每次工具输出作为不可信数据回填给模型，保留工具本身的原始契约。"""
            output = await execute(request)
            if isinstance(output, ToolMessage) and output.status != "error":
                output.content = wrap_untrusted_tool_output(str(output.content or ""))
            return output

        tool_node = ToolNode(
            tools,
            handle_tool_errors=_safe_tool_error,
            awrap_tool_call=wrap_tool_result,
        )

        async def agent(state: ChatGraphState) -> dict:
            reply = await bound_model.ainvoke(state["messages"])
            # 工具调用一律串行。即使供应商返回多个调用，也每轮只放行第一个，
            # 其余调用由下一轮在已获得结果的上下文中重新判断，避免办公写操作
            # 或客户端请求之间发生竞争。
            if reply.tool_calls:
                # Do not rebuild ``AIMessage``: reasoning-capable compatible
                # providers require their additional reasoning payload to be
                # replayed after tool results on the next model call.
                reply.tool_calls = [reply.tool_calls[0]]
            return {"messages": [reply]}

        async def before_tool(state: ChatGraphState) -> dict:
            message = state["messages"][-1]
            call = message.tool_calls[0]
            name = str(call.get("name") or "执行工具")
            call_id = str(call.get("id") or f"tool-{len(self.records) + 1}")
            self._emit({"type": "step", "id": call_id, "title": name, "status": "running", "tool": name})
            return {}

        async def after_tool(state: ChatGraphState) -> dict:
            tool_message = state["messages"][-1]
            if not isinstance(tool_message, ToolMessage):
                return {"tool_rounds": int(state.get("tool_rounds") or 0) + 1}
            name = str(tool_message.name or "执行工具")
            call_id = str(tool_message.tool_call_id or f"tool-{len(self.records) + 1}")
            result = self._tool_results.pop(0) if self._tool_results else None
            record = {
                "skill": name,
                "success": bool(result and result.success),
                "error_code": result.error_code if result else "INVALID_ARGS",
                "error": result.error if result else "工具参数不符合要求",
            }
            self.records.append(record)
            self._emit(
                {
                    "type": "step",
                    "id": call_id,
                    "title": name,
                    "status": "completed" if record["success"] else "failed",
                    "tool": name,
                    "output": (result.output[:1000] if result and result.success else ""),
                    "error": None if record["success"] else record["error"],
                }
            )
            return {"tool_rounds": int(state.get("tool_rounds") or 0) + 1}

        async def finish(state: ChatGraphState) -> dict:
            reply = await model.ainvoke(
                state["messages"]
                + [HumanMessage(content="工具调用次数已达上限，请仅基于已获得的信息直接给出最终回答。")]
            )
            return {"messages": [AIMessage(content=reply.content or "")]}

        def route_after_agent(state: ChatGraphState) -> str:
            message = state["messages"][-1]
            return "before_tool" if isinstance(message, AIMessage) and message.tool_calls else "end"

        def route_after_tool(state: ChatGraphState) -> str:
            return "finish" if int(state.get("tool_rounds") or 0) >= self.max_rounds else "agent"

        graph = StateGraph(ChatGraphState)
        graph.add_node("agent", agent)
        graph.add_node("before_tool", before_tool)
        graph.add_node("tools", tool_node)
        graph.add_node("after_tool", after_tool)
        graph.add_node("finish", finish)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", route_after_agent, {"before_tool": "before_tool", "end": END})
        graph.add_edge("before_tool", "tools")
        graph.add_edge("tools", "after_tool")
        graph.add_conditional_edges("after_tool", route_after_tool, {"agent": "agent", "finish": "finish"})
        graph.add_edge("finish", END)

        state = await graph.compile().ainvoke(
            {"messages": convert_to_messages(messages), "tool_rounds": 0}
        )
        final = ""
        for message in reversed(state.get("messages") or []):
            if isinstance(message, AIMessage) and not message.tool_calls:
                final = str(message.content or "")
                break
        return redact_server_text(final), self.records, self.citations
