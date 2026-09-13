"""受控工具图。

LangGraph 负责 ``model -> before_tool -> ToolNode -> after_tool -> model`` 的
消息流转。工具的授权、
审计、用户隔离和脱敏仍在 ``execute_tool_call`` 中执行；因此模型无法通过
LangChain 绕过 Lumi 的场景白名单或访问其他用户资源。
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Annotated, TypedDict
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, convert_to_messages
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from loguru import logger

from app.agents.langchain.models import get_chat_model, cached_tool_support, remember_tool_support
from app.agents.langchain.tools import make_skill_tool
from app.agents.skills.discovery import (
    ToolDiscoverySession,
    build_domain_groups,
    record_discovery,
)
from app.agents.skills.base import SkillResult
from app.agents.skills.mandatory_tools import apply_tool_window
from app.agents.skills.prompting import build_tool_selection_contract
from app.agents.skills.executor import (
    CapabilitySelection,
    get_capabilities_for_scene,
    get_chat_capabilities_with_trace,
    record_candidate_selection,
    selection_requires_escalation,
    select_capabilities_with_trace,
)
from app.core.agent_security import redact_server_text, wrap_untrusted_tool_output
from app.core.model_response import normalize_tool_response
from app.services.tool_output_pipeline import clean_assistant_text
from app.services.tool_output_projection import project_tool_output


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
        self.discovery_session = ToolDiscoverySession()
        self._clarification_requested = False

    def _emit(self, value: str | dict) -> None:
        if self.on_progress:
            self.on_progress(value)

    async def _on_tool_result(self, result: SkillResult) -> None:
        self._tool_results.append(result)
        # ToolOutput 的正式元数据位于 ``meta.citations``；metadata 只为旧
        # 插件构造输入保留。这里必须从规范信封读取，否则工具虽已执行，
        # 最终 SSE 的 citations 会被错误地置空。
        citations = list(getattr(getattr(result, "meta", None), "citations", None) or [])
        if not citations and isinstance(getattr(result, "metadata", None), dict):
            citations = list(result.metadata.get("citations") or [])
        if citations:
            self.citations.extend(citations)

    async def run(self, messages: list[dict] | list[BaseMessage]) -> tuple[str, list[dict], list[dict]]:
        """执行串行工具循环，返回旧 ``run_skill_loop`` 保持的三元组契约。"""
        await self.discovery_session.load(self.user_id, self.conversation_id)
        current_user_message = ""
        for message in reversed(messages):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "type", "")
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if role in {"user", "human"} and isinstance(content, str):
                current_user_message = content
                break
        # Office DAG already provides a request-scoped capability namespace.
        # Chat has a small shared allowlist, but still selects only the useful
        # candidates so adjacent tools do not compete in every model context.
        if self.chat_model is not None:
            selection = CapabilitySelection(
                capabilities=await get_capabilities_for_scene(self.scene, self.user_role, self.user_id),
                candidates=[],
                scene=self.scene,
                reason="explicit_model",
            )
        elif self.scene == "chat":
            selection = await get_chat_capabilities_with_trace(
                current_user_message, self.user_role, self.user_id,
            )
        else:
            selection = await select_capabilities_with_trace(
                current_user_message,
                self.scene,
                self.user_role,
                limit=3 if self.scene == "office" else 8,
                user_id=self.user_id,
        )
        capabilities = selection.capabilities
        # 四层快照的第 1/2 层：Catalog 全量 与 场景/权限过滤后。
        # （第 3/4 层在每次截断处由 apply_tool_window 记录。）
        catalog_snapshot = list(
            await get_capabilities_for_scene(self.scene, self.user_role, self.user_id)
        )
        eligible_snapshot = list(capabilities)
        # ``office`` is also used by the lightweight fact-assistance path.
        # That path never owns a Job/effect journal, so write and confirmation
        # tools must be removed in code rather than merely discouraged in its
        # prompt. Durable office workflows use their separate executor path.
        if self.scene == "office":
            capabilities = [
                item for item in capabilities
                if not item.write_op and not item.requires_confirmation
            ]
            # 公开资料请求先定位 L1 域，再在 research 域内展开动作工具。
            # 这里不再依赖一份“联网关键词”正则；领域描述和 intent_tags
            # 是唯一召回依据，命中失败才回退到兼容候选池。
            import re
            legal_public = [
                item for item in await get_capabilities_for_scene(self.scene, self.user_role, self.user_id)
                if not item.write_op and not item.requires_confirmation
            ]
            groups = build_domain_groups(legal_public)
            from app.agents.skills.discovery import search_domains
            matched_domains = search_domains(current_user_message, groups, limit=2)
            research_hit = any(group.name == "research" for group in matched_domains)
            if research_hit:
                has_url = bool(re.search(r"https?://|www\\.", current_user_message, re.I))
                allowed_public = {"web_search", "web_fetch"} if has_url else {"web_search"}
                narrowed = [item for item in legal_public if item.domain == "research" and item.name in allowed_public]
                if narrowed:
                    by_name = {item.name: item for item in narrowed}
                    capabilities = list(by_name.values())
                    selection = CapabilitySelection(
                        capabilities=capabilities,
                        candidates=[
                            {"name": item.name, "domain": "research", "version": item.version,
                             "score": 1.0, "bootstrap": False, "availability_hint": "available"}
                            for item in capabilities
                        ],
                        scene=self.scene, top_score=1.0, second_score=0.0,
                        score_margin=1.0, ambiguous=False, low_confidence=False,
                        reason="domain_match_research", routing_mode="domain_first",
                    )
        # 会话级 L2 缓存可恢复此前已展开的稳定工具；仍受本轮场景和授权池限制。
        if self.discovery_session.loaded_tools:
            legal_names = {item.name for item in await get_capabilities_for_scene(self.scene, self.user_role, self.user_id)}
            cached = [
                item for item in self.discovery_session.loaded_tools.values()
                if item.name in legal_names
                and item.status == "stable"
                and (self.scene != "office" or (not item.write_op and not item.requires_confirmation))
            ]
            by_name = {item.name: item for item in [*capabilities, *cached]}
            # 会话缓存合并**也是**一处截断：以前在这里用 [:8] 会把强制工具挤掉。
            capabilities, _ = apply_tool_window(
                list(by_name.values()),
                limit=8,
                scene=self.scene,
                catalog=catalog_snapshot,
                eligible=eligible_snapshot,
                layer="chat_graph.cache_merge",
            )
        if self.chat_model is None:
            # Domain-first discovery for the production path.  RAG/flat tool
            # search no longer chooses the final callable; it only supplies a
            # bounded domain namespace.  The model still makes the concrete
            # tool decision from the injected schemas.  Keep the old selector
            # result as a compatibility fallback when no domain evidence is
            # available (for generic conversational turns).
            legal_capabilities = await get_capabilities_for_scene(self.scene, self.user_role, self.user_id)
            if self.scene == "office":
                legal_capabilities = [
                    item for item in legal_capabilities
                    if not item.write_op and not item.requires_confirmation
                ]
            groups = build_domain_groups(legal_capabilities)
            from app.agents.skills.discovery import search_domains

            matched_domains = search_domains(current_user_message, groups, limit=2)
            if matched_domains:
                domain_names = {group.name for group in matched_domains}
                discovered = [
                    item for item in legal_capabilities
                    if item.domain in domain_names
                    and item.status == "stable"
                    and (self.scene != "office" or (not item.write_op and not item.requires_confirmation))
                ]
                # 域发现后的截断同样必须保强制工具（否则"域命中"反而把读工具挤掉）。
                discovered, _ = apply_tool_window(
                    discovered,
                    limit=8,
                    scene=self.scene,
                    catalog=catalog_snapshot,
                    eligible=eligible_snapshot,
                    layer="chat_graph.domain_discovery",
                )
                if discovered:
                    self.discovery_session.add(discovered)
                    record_discovery(
                        current_user_message,
                        groups=groups,
                        results=discovered,
                        scene=self.scene,
                        user_id=self.user_id,
                        job_id=self.conversation_id,
                        session=self.discovery_session,
                    )
                    await self.discovery_session.save(self.user_id, self.conversation_id)
                    by_name = {item.name: item for item in [*capabilities, *discovered]}
                    # 最终池：上限只约束可选工具，强制工具（workspace_navigator）永远保留。
                    capabilities, _final_window = apply_tool_window(
                        list(by_name.values()),
                        limit=8,
                        scene=self.scene,
                        catalog=catalog_snapshot,
                        eligible=eligible_snapshot,
                        ranked=list(by_name.values()),
                        layer="chat_graph.final",
                    )
        # 统一资源能力层（Phase 5）：模型可见面收敛（默认关闭 → 逐字等于改造前）。
        # 收敛只改"模型看到的名字"；实现名仍是执行与审计的真相源，解析在
        # ``executor._resolve_model_alias`` 一处完成。
        from app.agents.capabilities.resource_surface import collapse_for_surface

        surface_pairs = collapse_for_surface(capabilities)
        surface_enabled_now = len(surface_pairs) != len(capabilities) or any(
            display != str(getattr(item, "name", "") or "") for item, display in surface_pairs
        )
        # 工具链路诊断：把四层快照落成结构化日志（排障时不必再翻代码找是哪一层丢的）。
        # 只含工具名与状态，不含参数/正文，因此可以安全进日志。
        # 收敛打开时快照记录**对外名**——它才回答"模型这次拿到了什么"。
        try:
            from app.core.observability import record_tool_window_snapshot

            record_tool_window_snapshot(
                scene=self.scene,
                catalog=catalog_snapshot,
                eligible=eligible_snapshot,
                final=(
                    [display for _item, display in surface_pairs]
                    if surface_enabled_now
                    else capabilities
                ),
                layer="chat_graph",
                job_id=str(self.conversation_id or ""),
            )
        except Exception:  # noqa: BLE001 - 诊断失败绝不影响工具注入
            pass
        if selection_requires_escalation(selection, current_user_message):
            record_candidate_selection(
                selection, request=current_user_message, user_id=self.user_id,
                job_id=self.conversation_id, selection_round=1,
            )
            # 候选接近是内部路由信号，不应把实现细节暴露给用户并中止请求。
            # 保留有限的只读候选，交给模型依据工具契约完成最终裁决。
        tools = []
        for capability, display in surface_pairs:
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
                allowed_tools={item.name for item in capabilities},
                display_name=display,
            )
            if tool is not None:
                tools.append(tool)
        if not tools:
            record_candidate_selection(
                selection, request=current_user_message, user_id=self.user_id,
                job_id=self.conversation_id, selection_round=1,
            )
            return "", [], []

        model = self.chat_model or await get_chat_model(
            scene=self.scene,
            user_id=self.user_id,
            api_key=self.api_key,
            model=self.model_name,
            base_url=self.base_url,
            llm_config=self.llm_config,
        )
        # Some local Ollama models (notably qwen2.5vl:7b) reject bind_tools.
        # Keep a request-scoped fallback that asks the text model for a tiny
        # JSON decision, then feeds the same normalized call into ToolNode.
        bound_model = None
        cache_model = self.model_name or getattr(model, "model_name", None) or getattr(model, "model", None)
        cache_base = self.base_url or getattr(model, "openai_api_base", None) or getattr(model, "base_url", None)
        native_tools_supported = cached_tool_support(cache_model, cache_base) is not False
        # Keep chat on the same generated, registry-derived boundary contract
        # as Office ReAct.  Tool descriptions alone are not enough to explain
        # why adjacent candidates should or should not be used.
        selection_contract = SystemMessage(content=build_tool_selection_contract(capabilities))

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
            nonlocal bound_model, native_tools_supported
            if self._clarification_requested:
                self._clarification_requested = False
                clarification = SystemMessage(content=(
                    "上一步工具调用缺少必要输入或参数不合法。请不要再次调用工具、不要猜测值，"
                    "只向用户提出一个简洁明确的问题，询问需要补充的信息；不要输出内部错误码、"
                    "JSON Schema 或工具实现细节。"
                ))
                reply = await model.ainvoke([clarification, *state["messages"]])
                return {"messages": [AIMessage(content=clean_assistant_text(str(reply.content or "")))]}
            prompt_messages = [selection_contract, *state["messages"]]
            if native_tools_supported:
                try:
                    if bound_model is None:
                        bound_model = model.bind_tools(tools)
                    reply = await bound_model.ainvoke(prompt_messages)
                    remember_tool_support(cache_model, cache_base, True)
                except Exception as exc:
                    text = str(exc).casefold()
                    if "does not support tools" not in text and "tool calling" not in text and "bind_tools" not in text:
                        raise
                    native_tools_supported = False
                    remember_tool_support(cache_model, cache_base, False)
                    logger.warning("当前模型不支持原生工具调用，切换文本决策适配: {}", str(exc)[:160])
            if not native_tools_supported:
                fallback_contract = SystemMessage(content=(
                    str(selection_contract.content)
                    + "\n当前模型不支持原生工具调用。请只输出一个 JSON 对象："
                    + '{"name":"工具名","arguments":{}} 表示调用工具；或 '
                    + '{"answer":"最终回答"} 表示无需工具直接回答。不要输出 Markdown、解释或其他文本。'
                ))
                reply = await model.ainvoke([fallback_contract, *state["messages"]])
            clean_content, normalized_calls, _warnings = normalize_tool_response(
                reply.content, getattr(reply, "tool_calls", None)
            )
            if not normalized_calls and isinstance(reply.content, str):
                try:
                    payload = json.loads(reply.content.strip())
                    if isinstance(payload, dict) and payload.get("answer") is not None:
                        clean_content = str(payload.get("answer") or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            if normalized_calls:
                reply.content = clean_content
                reply.tool_calls = [
                    {
                        "name": str(call.get("function", {}).get("name") or ""),
                        "args": call.get("function", {}).get("arguments") or {},
                        "id": str(call.get("id") or ""),
                        "type": "tool_call",
                    }
                    for call in normalized_calls
                ]
            else:
                reply.content = clean_content
            # 工具调用一律串行。即使供应商返回多个调用，也每轮只放行第一个，
            # 其余调用由下一轮在已获得结果的上下文中重新判断，避免办公写操作
            # 或客户端请求之间发生竞争。
            if reply.tool_calls:
                # Do not rebuild ``AIMessage``: reasoning-capable compatible
                # providers require their additional reasoning payload to be
                # replayed after tool results on the next model call.
                reply.tool_calls = [reply.tool_calls[0]]
            # Chat has one request-level pool (unlike Office ReAct's per-round
            # refresh). Log what was injected even if the model chose no tool.
            record_candidate_selection(
                selection,
                request=current_user_message,
                user_id=self.user_id,
                job_id=self.conversation_id,
                selection_round=int(state.get("tool_rounds") or 0) + 1,
                model_called=(str(reply.tool_calls[0].get("name") or "") if reply.tool_calls else None),
            )
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
            if result and result.error_code in {"INVALID_PARAMS", "MISSING_PARAMETER", "INVALID_ARGS", "VALIDATION_ERROR"}:
                self._clarification_requested = True
            self._emit(
                {
                    "type": "step",
                    "id": call_id,
                    "title": name,
                    "status": "completed" if record["success"] else "failed",
                    "tool": name,
                    # structured ToolOutput 通常没有 legacy ``output`` 字段；
                    # 使用统一投影生成可展示的短摘要，避免把网页原文带入 SSE。
                    "output": (project_tool_output(result)[:1000] if result and result.success else ""),
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
        return clean_assistant_text(redact_server_text(final)), self.records, self.citations
