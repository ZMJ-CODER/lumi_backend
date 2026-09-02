"""通用原子步骤 Agent：一个 DAG 节点最多执行一次外部能力调用."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.orchestration.presentation import attach_display_result, working_text
from app.agents.skills.executor import execute_tool_call, get_tools_for_scene
from app.agents.skills.recovery import classify_model_error, decide_failure

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


class AtomicStepAgent(WorkerAgent):
    """不绑定角色白名单的步骤执行器.

    每个节点都能看到当前场景允许的全部 Skill（包括 system）和 MCP 工具，
    但一个节点最多调用一个工具。更多工作必须由规划器拆成下一个 DAG 节点。
    """

    name = "atomic_step"
    description = "通用原子步骤：可调用当前场景任一本地/system Skill 或 MCP 工具；每步最多一次工具调用"
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
        on the Skill, so adding a capability does not require another branch
        in this Agent.
        """
        schema = ((tool.get("function") or {}).get("parameters") or {})
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        if not isinstance(properties, dict) or not properties:
            return None
        from app.agents.skills.registry import SkillRegistry

        skill = SkillRegistry.get(selected_tool)
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
            execution_scope=ctx.job_id,
        )
        if not result.success:
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
                "approval_fingerprint": str(result.metadata.get("approval_fingerprint") or ""),
            }
        return attach_display_result(node, {
            "success": True,
            "content": (result.output or "步骤已完成").strip(),
            "output": result.output,
            "tool": selected_tool,
            "attempt": node.retries + 1,
            "method_chain": planned_tools,
            "tool_metadata": result.metadata,
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
            all_tools = await get_tools_for_scene(ctx.scene, ctx.user_role, ctx.user_id)
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
            return {
                "success": False,
                "error": f"无法为已规划工具补齐必要参数: {selected_tool}",
                "error_code": "MISSING_PARAMETER",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
            }
        return await self._execute_direct(
            node, ctx, selected_tool, direct_args, planned_tools, selected_index
        )
