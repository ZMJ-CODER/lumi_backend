"""ReAct 的**节点实现**：模型决策轮与收尾轮。

从 `react/runner.py` 的 ``run()`` 闭包里抽出。原先这两个节点用
闭包捕获 ``model`` / ``instruction`` / ``internal_docs`` / ``native_tools_supported``，
于是"状态机的一部分"藏在运行流程里、无法单独读；现在它们只读 run 开始前写好的
实例字段（``_model`` / ``_instruction`` / ``_internal_docs`` / ``_native_tools_supported``），
行为逐字不变。

```text
agent_node   一轮决策：候选窗（见 tool_window_pipeline）→ 绑定工具 → 调模型
             → 规范化 tool_calls → 记账选择轨迹
finish_node  达到轮数上限后收尾：要求模型基于已有结果直接给结论
```
"""

from __future__ import annotations

import json

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from loguru import logger

from app.agents.langchain.models import remember_tool_support
from app.agents.langchain.tools import make_skill_tool
from app.agents.orchestration.react.state import ReactState
from app.agents.orchestration.react.tool_window_pipeline import (
    add_bootstrap_primitives,
    ensure_document_discovery,
    expand_domain_window,
    merge_discovered,
)
from app.agents.skills.executor import (
    get_office_react_capabilities_with_trace,
    record_candidate_selection,
    selection_requires_escalation,
)
from app.agents.skills.prompting import build_tool_selection_contract
from app.platform.model.model_response import normalize_tool_response
from app.platform.security.agent_security import wrap_untrusted_tool_output
from app.services.tool_output_pipeline import clean_assistant_text


class AgentNodeMixin:
    """ReAct 的三个节点：决策轮、工具执行轮、收尾轮（混入 ``OfficeReactRunner``）。

    节点放在一处的原因很实际：它们共用同一批模块级依赖（``make_skill_tool`` /
    ``get_office_react_capabilities_with_trace`` …），拆到两个模块会让 monkeypatch
    目标分裂成两处——测试打一个、代码用另一个，"补丁没生效"会以极难排查的方式出现。
    """

    async def agent_node(self, state: ReactState) -> dict:
        if state.get("force_clarification"):
            clarification_prompt = SystemMessage(content=(
                "上一步工具调用缺少用户必须提供的信息。现在不要再调用业务工具、不要猜测或补全参数，"
                "只向用户提出一个简洁明确的问题，询问缺失信息；不要输出内部错误码或 JSON Schema。"
            ))
            reply = await self._model.ainvoke([clarification_prompt, *state.get("messages", [])])
            return {"messages": [AIMessage(content=clean_assistant_text(str(reply.content or "")))]}
        # 每一轮重新按当前任务和已失败工具收窄函数定义。模型只能看见
        # 本轮候选工具，不能依赖第一轮的宽工具包持续试错。
        recent = state.get("messages") or []
        observation = ""
        for message in reversed(recent):
            if isinstance(message, ToolMessage):
                # Feed the bounded, sanitized observation back into the
                # next decision.  Truncating to a few hundred chars
                # made compiler errors and missing-dependency hints
                # disappear, causing the agent to repeat the same
                # action instead of repairing it.
                observation = str(message.content or "")[:4000]
                break
        previous_tool = self.records[-1]["skill"] if self.records else ""
        route_text = self._instruction + (
            f"\n已执行工具：{previous_tool}\n最新观察：{observation}"
            if previous_tool or observation else ""
        )
        if self.domain_first and not self._requested_domains:
            from app.agents.skills.executor import CapabilitySelection

            selection = CapabilitySelection(
                capabilities=[], candidates=[], scene="office",
                reason="awaiting_domain_request", routing_mode="domain_first",
            )
        else:
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
            # 只读候选冲突交给模型在候选契约内裁决，禁止把内部
            # margin 信号直接转换成面向用户的失败消息。
        capabilities = selection.capabilities
        # 候选窗流水线（顺序固定，见 react/tool_window_pipeline.py）：
        # 领域收窄 → 探索原语 → L2 缓存合并 → 多文档盘点前提。
        capabilities = await expand_domain_window(self, capabilities)
        capabilities = await add_bootstrap_primitives(self, capabilities)
        capabilities = await merge_discovered(self, capabilities)
        capabilities, selection = await ensure_document_discovery(
            self, capabilities, selection, self._internal_docs
        )
        # 绑定了工作区且步骤面向本地内容时，按阶段把工作区能力并入候选窗
        # （读取→暂存修改→沙箱验证→提交），不再因 write_op 永久隐藏写工具。
        capabilities = await self._maybe_inject_workspace_stage_window(capabilities, route_text)
        # 统一资源能力层（Phase 5）：模型可见面收敛（默认关闭 → 逐字不变）。
        # 收敛只改"模型看到的名字"：这里记下 对外名 → 实现名 的映射，
        # 后面所有**基于名字的判断**（前置读取护栏、失败方法排除、去重键）
        # 一律走实现名，因此 `Write` 不会绕过"改前必须先读"。
        from app.agents.capabilities.views.resource_surface import collapse_for_surface

        surface_pairs = collapse_for_surface(capabilities)
        self._surface_alias = {
            display: capability.name
            for capability, display in surface_pairs
            if display != capability.name
        }
        visible_capabilities = [
            capability
            if display == capability.name
            else capability.model_copy(update={"name": display})
            for capability, display in surface_pairs
        ]
        allowed_impl_names = {str(item.name) for item in capabilities}
        tool_pairs = []
        for capability, display in surface_pairs:
            tool = await make_skill_tool(
                capability.name, user_id=self.user_id, scene="office",
                conversation_id=self.job_id, user_role=self.user_role,
                on_notify=self._emit, on_result=self._on_result,
                user_message=self.user_request, llm_config=self.llm_config,
                approval_context_sha256=self.approval_context_sha256,
                office_doc_ids=[str(item.get("doc_id")) for item in self._internal_docs],
                authorized_workspace_id=self.workspace_id,
                execution_scope=self.job_id,
                allowed_tools=allowed_impl_names,
                display_name=display,
            )
            if tool is not None:
                tool_pairs.append((display, tool))
        # 会话开始始终只暴露一个搜索原语；它不绕过场景和权限过滤。
        from langchain_core.tools import StructuredTool

        tool_pairs.append(("search_tools", StructuredTool.from_function(
            coroutine=self._search_tools,
            name="search_tools",
            description="按任务描述发现当前已授权工具",
        )))
        tool_pairs.append(("discover_domain", StructuredTool.from_function(
            coroutine=self._request_domain,
            name="discover_domain",
            description=(
                "声明下一步需要进入的业务领域；只申请工具边界，不执行实际操作。"
                "可选领域：network、document、data、development、system、desktop、schedule、communication、writing。"
            ),
        )))
        # Registry extensions and deferred discovery can return the
        # same capability more than once. LangChain/OpenAI rejects
        # duplicate tool names at bind time, so deduplicate at the
        # final boundary while preserving the first (highest-ranked)
        # definition.
        unique_pairs = []
        seen_names: set[str] = set()
        for tool_name, tool in tool_pairs:
            normalized_name = str(tool_name or "").strip()
            if not normalized_name or normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            unique_pairs.append((normalized_name, tool))
        tool_pairs = unique_pairs
        if not tool_pairs:
            return {"messages": [AIMessage(content="当前步骤没有可用工具，无法继续执行。")], "allowed_tools": []}
        allowed = [name for name, _ in tool_pairs]
        tools = [tool for _, tool in tool_pairs]
        self.toolsets.append(allowed)
        # The selection policy is generated from the same registry
        # fields that shaped the candidate pool; it cannot drift from
        # a separately hand-maintained prompt table.
        prompt_messages = [
            SystemMessage(content=build_tool_selection_contract(visible_capabilities)),
            *state["messages"],
        ]
        if self._native_tools_supported:
            try:
                reply = await self._model.bind_tools(tools).ainvoke(prompt_messages)
                remember_tool_support(self._cache_model, self._cache_base, True)
            except Exception as exc:
                text = str(exc).casefold()
                if "does not support tools" not in text and "tool calling" not in text and "bind_tools" not in text:
                    raise
                self._native_tools_supported = False
                remember_tool_support(self._cache_model, self._cache_base, False)
                logger.warning("办公模型不支持原生工具调用，切换文本决策适配: {}", str(exc)[:160])
        if not self._native_tools_supported:
            fallback_prompt = SystemMessage(content=(
                "你当前不能使用原生工具协议。请只输出一个 JSON 对象："
                '{"name":"工具名","arguments":{}} 表示调用一个工具；或 '
                '{"answer":"最终回答"} 表示无需工具直接回答。不要输出 Markdown、解释或其他文本。'
            ))
            reply = await self._model.ainvoke([fallback_prompt, *state["messages"]])
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

    async def execute_tool_node(self, state: ReactState) -> dict:
        """``tools`` 节点：执行本轮白名单工具（每轮动态工具集，不能复用静态 ToolNode）。"""
        message = state["messages"][-1]
        call = message.tool_calls[0]
        name = str(call.get("name") or "")
        args = call.get("args") or {}
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
        if name == "discover_domain":
            raw_args = call.get("args") or {}
            output = await self._request_domain(
                str(raw_args.get("domain") or ""),
                str(raw_args.get("reason") or ""),
                str(raw_args.get("mode") or "read_only"),
            )
            return {"messages": [ToolMessage(content=wrap_untrusted_tool_output(output), tool_call_id=call_id, name=name)]}
        # 收敛后的模型叫 ``Write``/``Edit``，而护栏、去重键、失败排除都按
        # **实现名**判断；先解析回实现名，再进入任何名字型逻辑。
        impl_name = self._impl_name(name)
        blocked = self.guard_tool_call(impl_name, args, call_id=call_id, name=name)
        if blocked:
            return {"messages": [ToolMessage(content=blocked, tool_call_id=call_id, name=name, status="error")]}
        tool = await make_skill_tool(
            impl_name, user_id=self.user_id, scene="office", conversation_id=self.job_id,
            user_role=self.user_role, on_notify=self._emit, on_result=self._on_result,
            user_message=self.user_request, llm_config=self.llm_config,
            approval_context_sha256=self.approval_context_sha256,
            office_doc_ids=[str(item.get("doc_id")) for item in self._internal_docs],
            authorized_workspace_id=self.workspace_id,
            execution_scope=self.job_id,
            # 白名单是**实现名**：执行器先解析对外名再校验（解析发生在一处）。
            allowed_tools={self._impl_name(item) for item in (state.get("allowed_tools") or [])},
            display_name=name if impl_name != name else "",
        )
        if tool is None:
            return {"messages": [ToolMessage(
                content="工具当前不可用，请更换一种方法。", tool_call_id=call_id,
                name=name, status="error",
            )]}
        try:
            output = await tool.ainvoke(args)
            self.mark_successful_read(impl_name, args)
            content = wrap_untrusted_tool_output(str(output or ""))
            return {"messages": [ToolMessage(content=content, tool_call_id=call_id, name=name)]}
        except Exception:
            return {"messages": [ToolMessage(
                content="工具调用未执行：请修正参数、换一种方法或说明限制。",
                tool_call_id=call_id, name=name, status="error",
            )]}

    async def finish_node(self, state: ReactState) -> dict:
        reply = await self._model.ainvoke(
            state["messages"] + [HumanMessage(content="已达到工具轮数上限。请基于已有结果直接给出最终回答，不要再调用工具。")]
        )
        return {"messages": [AIMessage(content=reply.content or "")]}


__all__ = ["AgentNodeMixin"]
