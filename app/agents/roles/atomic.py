"""通用原子步骤 Agent：一个 DAG 节点最多执行一次外部能力调用."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.orchestration.execution.presentation import attach_display_result, working_text
from app.agents.skills.executor import execute_tool_call, get_tools_for_scene
from app.agents.skills.recovery import classify_model_error, decide_failure
from app.services.tool_output_pipeline import render_for_model

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class AtomicStepAgent(WorkerAgent):
    """不绑定角色白名单的步骤执行器.

    每个节点都能看到当前场景允许的全部 Tool（包括 system）和 MCP 工具，
    但一个节点最多调用一个工具。更多工作必须由规划器拆成下一个 DAG 节点。
    """

    name = "atomic_step"
    description = "通用原子步骤：可调用当前场景任一本地/system Tool 或 MCP 工具；每步最多一次工具调用"
    params_help = (
        'params 用 {"instruction":"本步骤唯一目标", "preferred_tool":"可选工具名", '
        '"inputs":{}}；需要多次工具调用时必须拆成多个有依赖关系的步骤'
    )
    skills: list[str] = []

    @staticmethod
    def _direct_arguments(
        tool: dict,
        selected_tool: str,
        inputs: dict,
        instruction: str,
    ) -> dict | None:
        """Build a safe direct invocation from an already approved plan.

        The planner has selected exactly one capability.  Asking the model to
        express that same choice again with ``tool_choice`` is both redundant
        and incompatible with otherwise usable OpenAI-compatible endpoints.
        Prefer the concrete plan inputs.  The direct-execution contract lives
        on the Tool, so adding a capability does not require another branch
        in this Agent.
        """
        schema = ((tool.get("function") or {}).get("parameters") or {})
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        if not isinstance(properties, dict) or not properties:
            return None
        from app.agents.skills.registry import ToolRegistry

        skill = ToolRegistry.get(selected_tool)
        aliases = dict(getattr(skill, "direct_input_aliases", {}) or {}) if skill else {}
        direct = {}
        for key, value in (inputs.items() if isinstance(inputs, dict) else []):
            target = aliases.get(str(key), str(key))
            if target in properties:
                direct[target] = value

        instruction_field = str(getattr(skill, "direct_instruction_field", "") or "")
        if instruction_field and instruction_field in properties and instruction:
            direct.setdefault(instruction_field, instruction)

        required = list(getattr(skill, "direct_required_fields", []) or []) if skill else []
        if not required:
            required = list(schema.get("required") or [])
        # 未注册的第三方/MCP 工具没有直接执行声明时，不把空 Schema 误解为
        # 无参数能力；保留 JSON 参数提取路径，避免意外调用。
        if skill is None and not required:
            return None

        def has_value(value: object) -> bool:
            return value is not None and value != ""

        if all(name in properties and has_value(direct.get(name)) for name in required):
            return direct
        return None

    @staticmethod
    def _missing_planned_inputs(selected_tool: str, inputs: object) -> list[str]:
        """Return only Tool-declared, non-inferable inputs absent from a plan."""
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(selected_tool)
        values = inputs if isinstance(inputs, dict) else {}
        return [
            str(field)
            for field in (getattr(tool, "plan_required_fields", None) or [])
            if str(field).strip() and values.get(str(field)) in (None, "", [])
        ]

    @staticmethod
    async def _execute_direct(
        node: "TaskNode",
        ctx: WorkerContext,
        selected_tool: str,
        direct_args: dict,
        planned_tools: list[str],
        selected_index: int,
    ) -> dict:
        call = {
            "id": f"direct-{node.id}",
            "type": "function",
            "function": {
                "name": selected_tool,
                "arguments": json.dumps(direct_args, ensure_ascii=False),
            },
        }
        # A desktop staged commit first returns a non-blocking pending result.
        # Once the orchestrator's existing approval gate resumes this node,
        # the exact same tool call is reissued with an explicit approval bit.
        if selected_tool == "sandbox_commit":
            # Keep the exact version that the user reviewed.  Node lifecycle
            # preserves the pending execution envelope while the Job is in
            # WAITING_APPROVAL, so a resumed node must not silently obtain a
            # newer version and commit a different workspace state.
            pending_execution = (node.result or {}).get("execution") if isinstance(node.result, dict) else {}
            pending_data = pending_execution.get("data") if isinstance(pending_execution, dict) else {}
            if ctx.confirmed_tool_calls:
                direct_args = {
                    **direct_args,
                    "approved": True,
                    "base_version": (pending_data or {}).get("base_version", direct_args.get("base_version")),
                    "idempotency_key": str(direct_args.get("idempotency_key") or f"{ctx.job_id}:{node.id}:workspace_commit"),
                }
        result = await execute_tool_call(
            call,
            ctx.user_id,
            ctx.scene,
            ctx.job_id,
            user_role=ctx.user_role,
            user_message=ctx.user_request,
            llm_api_key=ctx.llm_api_key,
            llm_config=ctx.llm_config,
            confirmed_tools=ctx.confirmed_tools,
            confirmed_tool_calls=ctx.confirmed_tool_calls,
            approval_context_sha256=ctx.approval_context_sha256,
            on_output=ctx.on_output,
            office_doc_ids=ctx.office_doc_ids,
            authorized_project_ids=ctx.authorized_project_ids,
            authorized_workspace_id=ctx.workspace_id,
            execution_scope=ctx.job_id,
            allow_internal=True,
        )
        if result.status == "pending_approval":
            from app.agents.skills.executor import tool_call_fingerprint

            fingerprint = tool_call_fingerprint(selected_tool, direct_args, ctx.approval_context_sha256)
            return {
                "success": False,
                "error": result.meta.summary or "变更正在等待用户审批",
                "error_code": "NEEDS_CONFIRMATION",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "retryable": False,
                "approval_fingerprint": fingerprint,
                "tool_metadata": {"approval_fingerprint": fingerprint, "call_id": result.call_id or ""},
                "execution": result.to_execution_envelope(),
            }
        if result.status == "failed":
            decision = decide_failure(
                result.error_code,
                result.error,
                retryable=result.retryable,
                alternatives_remaining=selected_index + 1 < len(planned_tools),
            )
            return {
                "success": False,
                "error": result.error or f"工具 {selected_tool} 执行失败",
                "error_code": result.error_code or "EXEC_ERROR",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "retryable": decision.retry_same or decision.try_alternative,
                "use_next_tool": decision.try_alternative,
                "recovery_category": decision.category,
                "replan_required": decision.replan_required,
                "approval_fingerprint": str(result.meta.quality_hints.get("approval_fingerprint") or ""),
                "execution": result.to_execution_envelope(),
            }
        # 工作区读取是交给下游节点的**证据**，不是一句 UI 状态。旧的通用 2200 字符
        # 展示预算会把长 PPT/DOCX 的尾部在 direct_llm 回答前就丢掉。普通工具保持紧凑
        # 预算；工作区读取域（聚合入口 + 覆盖 Agent）使用配置的上下文窗口。
        display_limit = 2200
        from app.workspace.context import WORKSPACE_READ_TOOL_NAMES
        from app.workspace.read.navigator import handoff_text

        # 覆盖读取（workspace_coverage）同样产出工作区正文证据，必须一起豁免：
        # 只放行 navigator+read 会让覆盖链路被 2200 字符截断，模型只拿到半段正文，
        # 于是回答"尚未读取到文件正文"。
        is_workspace_read = selected_tool in WORKSPACE_READ_TOOL_NAMES or selected_tool == "workspace_coverage"
        if is_workspace_read:
            from app.core.config import settings

            display_limit = max(
                display_limit,
                int(getattr(settings, "WORKSPACE_READ_CONTEXT_MAX_CHARS", 120000)),
            )
        # 交接优先级：
        #   1) 覆盖 Agent 这类**自己已渲染好正文**的执行体：直接透传 output
        #      （它自己已按 MAX_EVIDENCE_CHARS / MAX_TOTAL_CHARS 控过预算）；
        #   2) navigator 的统一信封：用 handoff_text 渲染成可读正文证据
        #      （而不是把整包 JSON 塞给下游，让正文埋在 limits/parser 等噪音里）。
        direct_evidence = ""
        payload = result.data if isinstance(result.data, dict) else {}
        if selected_tool == "workspace_coverage":
            direct_evidence = str(getattr(result, "output", "") or "").strip()
        if not direct_evidence and selected_tool in WORKSPACE_READ_TOOL_NAMES:
            direct_evidence = handoff_text(payload, limit=display_limit)
        if not direct_evidence and selected_tool == "workspace_coverage":
            direct_evidence = handoff_text(payload, limit=display_limit)
        if direct_evidence:
            return attach_display_result(node, {
                "success": True,
                "content": direct_evidence[:display_limit],
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "execution": result.to_execution_envelope(),
                "step_title": node.name or str(node.params.get("instruction") or "")[:40],
                "read_evidence": True,
            })
        return attach_display_result(node, {
            "success": True,
            "content": render_for_model(result, max_chars=display_limit).strip(),
            "tool": selected_tool,
            "attempt": node.retries + 1,
            "method_chain": planned_tools,
            "execution": result.to_execution_envelope(),
            "step_title": node.name or str(node.params.get("instruction") or "")[:40],
        })

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        instruction = str(node.params.get("instruction") or node.name or "").strip()
        if not instruction:
            return {
                "success": False,
                "error": "原子步骤缺少 instruction",
                "error_code": "INVALID_ARGS",
            }

        await _report_progress(ctx.job_id, node.id, working_text(node))

        preferred = str(node.params.get("preferred_tool") or "").strip()
        if not preferred:
            return {
                "success": False,
                "error": "原子步骤必须由 Planner 唯一指定 preferred_tool",
                "error_code": "TOOL_NOT_PLANNED",
            }
        fallback_tools = [
            str(name).strip()
            for name in (node.params.get("fallback_tools") or [])
            if str(name).strip() and str(name).strip() != preferred
        ]
        planned_tools = [preferred, *fallback_tools]
        # ``retries`` 不等于“换工具次数”：网络超时等情况应重试原方法。
        # 只有 DAG 引擎收到 use_next_tool 时才递增 tool_index。
        # 兼容旧任务快照：旧引擎只保存 retries，表示第 N 个备用方法。
        # 新引擎在首次执行前写入 tool_index=0，使暂态重试不会意外切换工具。
        raw_tool_index = (node.metadata or {}).get("tool_index")
        selected_index = min(
            int(raw_tool_index) if raw_tool_index is not None else node.retries,
            len(planned_tools) - 1,
        )
        selected_tool = planned_tools[selected_index]
        # ``user_id`` enables per-user MCP bindings in production.  Keep a
        # narrow compatibility fallback for older plugin/test providers that
        # still expose the original two-argument discovery contract.
        try:
            all_tools = await get_tools_for_scene(
                ctx.scene, ctx.user_role, ctx.user_id, include_internal=True
            )
        except TypeError as exc:
            if "positional" not in str(exc) and "argument" not in str(exc):
                raise
            all_tools = await get_tools_for_scene(ctx.scene, ctx.user_role)
        tools = [
            tool
            for tool in all_tools
            if str(tool.get("function", {}).get("name") or "") == selected_tool
        ]
        if not tools:
            return {
                "success": False,
                "error": f"规划工具不可用或不允许用于当前场景: {selected_tool}",
                "error_code": "SKILL_NOT_FOUND",
            }
        inputs = node.params.get("inputs") or {}
        missing_plan_inputs = self._missing_planned_inputs(selected_tool, inputs)
        if missing_plan_inputs:
            return {
                "success": False,
                "error": (
                    "计划缺少必须由用户或规划器明确指定的目标信息："
                    + "、".join(missing_plan_inputs)
                    + "。系统不会猜测目标，请补充后重试。"
                ),
                "error_code": "MISSING_PARAMETER",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "retryable": False,
            }
        direct_args = self._direct_arguments(tools[0], selected_tool, inputs, instruction)
        if direct_args is not None:
            return await self._execute_direct(
                node, ctx, selected_tool, direct_args, planned_tools, selected_index
            )
        # 计划缺少必要参数时，进行一次受控 JSON 参数提取，而不是强制模型
        # Function Calling。许多 OpenAI-compatible 端点支持聊天和 JSON，
        # 但不支持 tool_choice 方言；实际工具仍只会执行 Planner 指定的一个。
        from app.agents.langchain.agent import extract_tool_arguments

        try:
            extracted = await extract_tool_arguments(
                instruction=instruction,
                explicit_inputs=inputs,
                dependency_results=node.metadata.get("dependency_results") or {},
                tool_definition=tools[0],
                scene=ctx.scene,
                user_id=ctx.user_id,
                api_key=ctx.llm_api_key,
                llm_config=ctx.llm_config,
            )
        except Exception as exc:  # noqa: BLE001
            error_code, user_error = classify_model_error(exc)
            return {
                "success": False,
                "error": user_error,
                "error_code": error_code,
                "retryable": False,
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
            }
        direct_args = self._direct_arguments(tools[0], selected_tool, extracted, instruction)
        if direct_args is None:
            missing_plan_inputs = self._missing_planned_inputs(selected_tool, extracted)
            missing = "、".join(missing_plan_inputs) if missing_plan_inputs else "工具所需参数"
            return {
                "success": False,
                "error": f"计划缺少必要输入：{missing}。系统不会猜测目标，请补充后重试。",
                "error_code": "MISSING_PARAMETER",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
            }
        return await self._execute_direct(
            node, ctx, selected_tool, direct_args, planned_tools, selected_index
        )
