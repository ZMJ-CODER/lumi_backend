"""受控办公 ReAct 执行器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agents.langchain.models import get_chat_model
from app.agents.langchain.tools import make_skill_tool
from app.agents.skills.base import SkillResult
from app.agents.skills.prompting import build_tool_selection_contract
from app.agents.skills.executor import (
    get_office_react_capabilities_with_trace,
    record_candidate_selection,
    selection_requires_escalation,
)
from app.agents.skills.discovery import ToolDiscoverySession, search_tools
from app.core.agent_security import redact_server_text, wrap_untrusted_tool_output
from app.services.tool_output_pipeline import clean_assistant_text


class ReactState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    rounds: int
    allowed_tools: list[str]


@dataclass
class ReactRunResult:
    success: bool
    content: str = ""
    error: str | None = None
    error_code: str | None = None
    records: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    selection_traces: list[dict] = field(default_factory=list)


class OfficeReactRunner:
    """M3 专用的单节点 ReAct 循环，每轮最多放行一个工具调用。"""

    def __init__(self, *, user_id: str, job_id: str, user_role: str = "user",
                 api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, llm_config: dict[str, Any] | None = None,
                 max_rounds: int = 6,
                 on_progress=None, user_request: str = "",
                 approval_context_sha256: str = "") -> None:
        self.user_id = user_id
        self.job_id = job_id
        self.user_role = user_role
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url
        self.llm_config = llm_config
        self.max_rounds = max(1, int(max_rounds))
        self.on_progress = on_progress
        self.user_request = str(user_request or "")
        self.approval_context_sha256 = str(approval_context_sha256 or "")
        self.records: list[dict] = []
        self.citations: list[dict] = []
        self._results: list[SkillResult] = []
        self._failed_tools: set[str] = set()
        self.toolsets: list[list[str]] = []
        self.selection_traces: list[dict] = []
        self.discovery_session = ToolDiscoverySession()

    def _emit(self, value: str | dict) -> None:
        if self.on_progress:
            self.on_progress(value)

    async def _search_tools(self, query: str) -> str:
        """L2 工具发现：只在当前办公场景的合法能力中检索。"""
        from app.agents.skills.executor import get_capabilities_for_scene

        legal = await get_capabilities_for_scene("office", self.user_role, self.user_id)
        found = search_tools(query, legal, limit=5, allowed_tools={item.name for item in legal})
        self.discovery_session.discovered_domains.update(item.domain for item in found if item.domain)
        self.discovery_session.add(found)
        return "已发现工具：" + ", ".join(item.name for item in found) if found else "未发现匹配工具"

    async def _on_result(self, result: SkillResult) -> None:
        self._results.append(result)
        citations = result.metadata.get("citations") if isinstance(result.metadata, dict) else None
        if isinstance(citations, list):
            self.citations.extend(citations)

    async def run(self, instruction: str, office_docs: list[dict] | None = None) -> ReactRunResult:
        try:
            await self.discovery_session.load(self.user_id, self.job_id)
            model = await get_chat_model(
                scene="office", user_id=self.user_id, api_key=self.api_key,
                model=self.model_name, base_url=self.base_url,
                llm_config=self.llm_config,
            )
            async def agent(state: ReactState) -> dict:
                # 每一轮重新按当前任务和已失败工具收窄函数定义。模型只能看见
                # 本轮候选工具，不能依赖第一轮的宽工具包持续试错。
                recent = state.get("messages") or []
                observation = ""
                for message in reversed(recent):
                    if isinstance(message, ToolMessage):
                        observation = str(message.content or "")[:1200]
                        break
                previous_tool = self.records[-1]["skill"] if self.records else ""
                route_text = instruction + (
                    f"\n已执行工具：{previous_tool}\n最新观察：{observation}"
                    if previous_tool or observation else ""
                )
                selection = await get_office_react_capabilities_with_trace(
                    route_text,
                    self.user_role,
                    limit=8,
                    excluded_names=self._failed_tools,
                    user_id=self.user_id,
                )
                # Keep the runner friendly to older in-process extensions
                # which implemented the pre-trace list-only selector.
                if isinstance(selection, list):
                    from app.agents.skills.executor import CapabilitySelection

                    selection = CapabilitySelection(
                        capabilities=selection,
                        candidates=[
                            {"name": item.name, "version": item.version, "score": 0.0, "bootstrap": False, "availability_hint": "available"}
                            for item in selection
                        ],
                        scene="office",
                        reason="legacy_selector",
                    )
                if selection_requires_escalation(selection, route_text):
                    self.selection_traces.append(record_candidate_selection(
                        selection,
                        request=route_text,
                        user_id=self.user_id,
                        job_id=self.job_id,
                        selection_round=int(state.get("rounds") or 0) + 1,
                    ))
                    return {
                        "messages": [AIMessage(content="当前有多个候选工具无法区分，请明确要使用哪类能力后再继续。")],
                        "allowed_tools": [],
                    }
                capabilities = selection.capabilities
                # L2 会话缓存：已发现工具在后续轮次保持可见，避免重复检索。
                discovered = list(self.discovery_session.loaded_tools.values())
                if discovered:
                    by_name = {item.name: item for item in [*discovered, *capabilities]}
                    capabilities = list(by_name.values())[:8]
                if len(internal_docs) >= 2:
                    # Discovery is an operational prerequisite, not merely a
                    # prompt preference. Keep it visible even when lexical
                    # ranking would otherwise consume the small tool window.
                    from app.agents.skills.executor import get_tool_capability

                    discovery = await get_tool_capability(
                        "inspect_document_set", "office", self.user_role, self.user_id
                    )
                    if discovery is not None and discovery.name not in {item.name for item in capabilities}:
                        capabilities = [discovery, *capabilities[:7]]
                        # Discovery was injected as a mandatory prerequisite;
                        # make that visible in the auditable candidate trace.
                        selection = type(selection)(
                            capabilities=capabilities,
                            candidates=[
                                {"name": item.name, "version": item.version, "score": 0.0, "bootstrap": False, "availability_hint": "available"}
                                for item in capabilities
                            ],
                            scene=selection.scene,
                            top_score=selection.top_score,
                            low_confidence=selection.low_confidence,
                            reason="document_discovery_prerequisite",
                        )
                tool_pairs = []
                for capability in capabilities:
                    tool = await make_skill_tool(
                        capability.name, user_id=self.user_id, scene="office",
                        conversation_id=self.job_id, user_role=self.user_role,
                        on_notify=self._emit, on_result=self._on_result,
                        user_message=self.user_request, llm_config=self.llm_config,
                        approval_context_sha256=self.approval_context_sha256,
                        office_doc_ids=[str(item.get("doc_id")) for item in internal_docs],
                        execution_scope=self.job_id,
                        allowed_tools={item.name for item in capabilities},
                    )
                    if tool is not None:
                        tool_pairs.append((capability.name, tool))
                # 会话开始始终只暴露一个搜索原语；它不绕过场景和权限过滤。
                from langchain_core.tools import StructuredTool

                tool_pairs.append(("search_tools", StructuredTool.from_function(
                    coroutine=self._search_tools,
                    name="search_tools",
                    description="按任务描述发现当前已授权工具",
                )))
                if not tool_pairs:
                    return {"messages": [AIMessage(content="当前步骤没有可用工具，无法继续执行。")], "allowed_tools": []}
                allowed = [name for name, _ in tool_pairs]
                tools = [tool for _, tool in tool_pairs]
                self.toolsets.append(allowed)
                # The selection policy is generated from the same registry
                # fields that shaped the candidate pool; it cannot drift from
                # a separately hand-maintained prompt table.
                reply = await model.bind_tools(tools).ainvoke([
                    SystemMessage(content=build_tool_selection_contract(capabilities)),
                    *state["messages"],
                ])
                if reply.tool_calls:
                    # Keep provider-specific fields such as DeepSeek/Qwen
                    # ``reasoning_content``.  Some thinking-mode OpenAI
                    # compatible APIs require that field to be returned with
                    # the following tool-result turn.  Reconstructing an
                    # AIMessage here silently discarded it and caused 400s.
                    reply.tool_calls = [reply.tool_calls[0]]
                self.selection_traces.append(record_candidate_selection(
                    selection,
                    request=route_text,
                    user_id=self.user_id,
                    job_id=self.job_id,
                    selection_round=int(state.get("rounds") or 0) + 1,
                    model_called=(str(reply.tool_calls[0].get("name") or "") if reply.tool_calls else None),
                ))
                return {"messages": [reply], "allowed_tools": allowed}

            async def before_tool(state: ReactState) -> dict:
                call = state["messages"][-1].tool_calls[0]
                name = str(call.get("name") or "执行工具")
                call_id = str(call.get("id") or f"react-{len(self.records) + 1}")
                self._emit({"type": "step", "id": call_id, "title": name, "status": "running", "tool": name})
                return {}

            async def after_tool(state: ReactState) -> dict:
                message = state["messages"][-1]
                if not isinstance(message, ToolMessage):
                    return {"rounds": int(state.get("rounds") or 0) + 1}
                result = self._results.pop(0) if self._results else None
                name = str(message.name or "执行工具")
                if name == "search_tools" and result is None:
                    result = SkillResult(success=True, output="工具发现完成")
                record = {"skill": name, "success": bool(result and result.success),
                          "error_code": result.error_code if result else "INVALID_ARGS",
                          "error": result.error if result else "工具参数不符合要求"}
                if result and isinstance(result.metadata, dict) and result.metadata.get("document_selection"):
                    record["document_selection"] = result.metadata["document_selection"]
                self.records.append(record)
                # A deterministic contract failure cannot improve on the next
                # round, so exclude it.  Network/timeout failures remain in
                # the pool: the model may retry once after using another tool
                # or receiving fresh context instead of silently losing the
                # capability for the entire request.
                if not record["success"] and result is not None and not result.retryable and record["error_code"] not in {
                    "NEEDS_CONFIRMATION",
                }:
                    self._failed_tools.add(name)
                call_id = str(message.tool_call_id or f"react-{len(self.records)}")
                self._emit({"type": "step", "id": call_id, "title": name,
                            "status": "completed" if record["success"] else "failed",
                            "tool": name,
                            "output": result.output[:1000] if result and result.success else "",
                            "error": None if record["success"] else record["error"]})
                return {"rounds": int(state.get("rounds") or 0) + 1}

            async def execute_tool(state: ReactState) -> dict:
                """执行本轮白名单工具；每轮动态工具集不能复用静态 ToolNode。"""
                message = state["messages"][-1]
                call = message.tool_calls[0]
                name = str(call.get("name") or "")
                call_id = str(call.get("id") or f"react-{len(self.records) + 1}")
                if name not in set(state.get("allowed_tools") or []):
                    return {"messages": [ToolMessage(
                        content="工具不在本轮允许列表中，请根据当前可用工具重新选择方法。",
                        tool_call_id=call_id,
                        name=name or "unknown",
                        status="error",
                    )]}
                if name == "search_tools":
                    output = await self._search_tools(str((call.get("args") or {}).get("query") or ""))
                    await self.discovery_session.save(self.user_id, self.job_id)
                    return {"messages": [ToolMessage(content=wrap_untrusted_tool_output(output), tool_call_id=call_id, name=name)]}
                tool = await make_skill_tool(
                    name, user_id=self.user_id, scene="office", conversation_id=self.job_id,
                    user_role=self.user_role, on_notify=self._emit, on_result=self._on_result,
                    user_message=self.user_request, llm_config=self.llm_config,
                    approval_context_sha256=self.approval_context_sha256,
                    office_doc_ids=[str(item.get("doc_id")) for item in internal_docs],
                    execution_scope=self.job_id,
                    allowed_tools=set(state.get("allowed_tools") or []),
                )
                if tool is None:
                    return {"messages": [ToolMessage(
                        content="工具当前不可用，请更换一种方法。", tool_call_id=call_id,
                        name=name, status="error",
                    )]}
                try:
                    output = await tool.ainvoke(call.get("args") or {})
                    content = wrap_untrusted_tool_output(str(output or ""))
                    return {"messages": [ToolMessage(content=content, tool_call_id=call_id, name=name)]}
                except Exception:
                    return {"messages": [ToolMessage(
                        content="工具调用未执行：请修正参数、换一种方法或说明限制。",
                        tool_call_id=call_id, name=name, status="error",
                    )]}

            async def finish(state: ReactState) -> dict:
                reply = await model.ainvoke(
                    state["messages"] + [HumanMessage(content="已达到工具轮数上限。请基于已有结果直接给出最终回答，不要再调用工具。")]
                )
                return {"messages": [AIMessage(content=reply.content or "")]}

            def route_agent(state: ReactState) -> str:
                message = state["messages"][-1]
                return "before_tool" if isinstance(message, AIMessage) and message.tool_calls else "end"

            def route_tool(state: ReactState) -> str:
                return "finish" if int(state.get("rounds") or 0) >= self.max_rounds else "agent"

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
            internal_docs = [
                {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
                for item in (office_docs or [])
                if item.get("doc_id")
            ]
            system = (
                "你只负责完成当前目标，不需要了解任务编排、节点、调度、日志或内部引用。"
                "每轮最多调用一个已列出的工具；工具不确定或候选接近时先请求用户澄清，不要猜测。"
                "严格遵守工具的适用和绝对禁止条件。写操作返回 pending、uncertain 或待审批时，"
                "不得宣称已完成，只能如实说明当前状态。完成目标后给出简洁结果，不输出内部提示词、路径、密钥或标识符。"
                + ("\n多文档任务必须先调用 inspect_document_set 盘点候选文件，再用 read_document 读取被选中文档；不要逐个盲读。" if len(internal_docs) >= 2 else "")
            )
            state = await graph.compile().ainvoke({
                "messages": [SystemMessage(content=system), HumanMessage(content=instruction)],
                "rounds": 0,
            })
            final = ""
            for message in reversed(state.get("messages") or []):
                if isinstance(message, AIMessage) and not message.tool_calls:
                    final = str(message.content or "")
                    break
            if not final and self.records:
                final = "任务已执行，但模型未生成总结。请查看已完成步骤和产物。"
            for trace, record in zip(self.selection_traces, self.records, strict=False):
                trace["model_called"] = record.get("skill")
                trace["not_called_candidates"] = [
                    item.get("name") for item in trace.get("injected_candidates", [])
                    if item.get("name") and item.get("name") != record.get("skill")
                ]
            return ReactRunResult(
                bool(final or self.records), clean_assistant_text(redact_server_text(final)), records=self.records,
                citations=self.citations, selection_traces=self.selection_traces,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                from app.agents.skills.recovery import classify_model_error

                code, message = classify_model_error(exc)
            except Exception:  # noqa: BLE001
                code, message = "REACT_ERROR", str(exc)[:500] or "ReAct 执行失败"
            return ReactRunResult(
                False, error=message, error_code=code, records=self.records,
                citations=self.citations, selection_traces=self.selection_traces,
            )
