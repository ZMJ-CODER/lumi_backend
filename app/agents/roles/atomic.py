"""通用原子步骤 Agent：一个 DAG 节点最多执行一次外部能力调用."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.orchestration.presentation import attach_display_result, working_text
from app.agents.skills.executor import execute_tool_call, get_tools_for_scene
from app.agents.skills.recovery import classify_model_error, decide_failure
from app.core.agent_security import UNTRUSTED_CONTENT_RULES, redact_server_text, wrap_untrusted_tool_output

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
        dependency_results = node.metadata.get("dependency_results") or {}
        inputs = node.params.get("inputs") or {}
        # 目标与参数已由 Planner 确定的读取类工具不需要模型“再选一次”。
        # 否则模型余额/配置故障会阻断本可直接完成的知识库或文档读取。
        if selected_tool in {"office_doc_read", "office_doc_analyze", "query_knowledge", "get_datetime"}:
            direct_args = dict(inputs)
            if selected_tool == "office_doc_read":
                direct_args = {"doc_id": direct_args.get("doc_id")}
            else:
                if selected_tool == "office_doc_analyze":
                    direct_args = {
                        "doc_id": direct_args.get("doc_id"),
                        "instruction": direct_args.get("instruction") or instruction,
                        "mode": direct_args.get("analyze_mode") or direct_args.get("mode") or "qa",
                    }
                elif selected_tool == "query_knowledge":
                    direct_args = {
                        "query": direct_args.get("query") or instruction,
                        "top_k": direct_args.get("top_k") or 5,
                    }
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
                }
            return attach_display_result(node, {
                "success": True,
                "content": (result.output or "步骤已完成").strip(),
                "output": result.output,
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "tool_metadata": result.metadata,
                "step_title": node.name or instruction[:40],
            })
        from app.agents.langchain.agent import choose_single_tool
        from app.agents.langchain.tools import make_skill_tool

        langchain_tool = await make_skill_tool(
            selected_tool,
            user_id=ctx.user_id,
            scene=ctx.scene,
            conversation_id=ctx.job_id,
            user_role=ctx.user_role,
        )
        if langchain_tool is None:
            return {
                "success": False,
                "error": f"规划工具不可用或不允许用于当前场景: {selected_tool}",
                "error_code": "SKILL_NOT_FOUND",
            }
        system = (
            "你正在执行 DAG 中的一个原子步骤。只完成本步骤目标，不扩展到其他步骤。"
            "本次尝试已经唯一指定一个工具；只允许调用所提供的这个工具，且最多调用一次。"
            "如果这是备用方法，请根据工具能力采用与前一次不同的实现路径。"
            "不要重复执行依赖步骤已经完成的副作用。"
            "\n\n" + UNTRUSTED_CONTENT_RULES
        )
        user = (
            f"步骤：{instruction}\n"
            f"显式输入：{json.dumps(inputs, ensure_ascii=False, default=str)[:12000]}\n"
            f"依赖步骤结构化结果：{json.dumps(dependency_results, ensure_ascii=False, default=str)[:24000]}\n"
            + f"本次唯一允许工具：{selected_tool}\n"
            + f"本步骤方法链：{json.dumps(planned_tools, ensure_ascii=False)}\n"
            + f"当前为第 {node.retries + 1} 次尝试"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            content, tool_calls = await choose_single_tool(
                system=system,
                user=user,
                tool=langchain_tool,
                scene=ctx.scene,
                user_id=ctx.user_id,
                api_key=ctx.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            # 不再绕回第二套 function-calling。交由 LangGraphNodeRunner 按统一
            # 恢复策略重试、换备用工具或要求重新规划。
            error_code, user_error = classify_model_error(exc)
            return {
                "success": False,
                "error": user_error,
                "error_code": error_code,
                "retryable": error_code == "MODEL_UNAVAILABLE",
                "tool": selected_tool,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
            }
        if not tool_calls:
            return {
                "success": False,
                "error": f"原子步骤未调用本次指定的工具: {selected_tool}",
                "error_code": "TOOL_NOT_CALLED",
            }

        if len(tool_calls) != 1:
            return {
                "success": False,
                "error": "原子步骤返回了多个工具调用，已拒绝执行",
                "error_code": "NON_ATOMIC_TOOL_CALL",
            }
        call = tool_calls[0]
        called_name = str((call.get("function") or {}).get("name") or "")
        if called_name != selected_tool:
            return {
                "success": False,
                "error": f"原子步骤试图调用未授权工具: {called_name}",
                "error_code": "TOOL_NOT_ALLOWED",
            }
        result = await execute_tool_call(
            call,
            ctx.user_id,
            ctx.scene,
            ctx.job_id,
            user_role=ctx.user_role,
        )
        tool_name = called_name
        if not result.success:
            decision = decide_failure(
                result.error_code,
                result.error,
                retryable=result.retryable,
                alternatives_remaining=selected_index + 1 < len(planned_tools),
            )
            return {
                "success": False,
                "error": result.error or f"工具 {tool_name} 执行失败",
                "error_code": result.error_code or "EXEC_ERROR",
                "tool": tool_name,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "retryable": decision.retry_same or decision.try_alternative,
                "use_next_tool": decision.try_alternative,
                "recovery_category": decision.category,
                "replan_required": decision.replan_required,
            }

        messages.extend(
            [
                {"role": "assistant", "content": content or None, "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": wrap_untrusted_tool_output(result.output),
                },
                {
                    "role": "user",
                    "content": "基于工具结果简洁总结本步骤产出，不要继续调用工具。",
                },
            ]
        )
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_SKILL

        llm = LLMClient()
        try:
            final = await llm.chat(
                messages,
                scene=ctx.scene,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_SKILL,
                api_key=ctx.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            # 工具已经成功时保留原始工具产出，避免最终措辞模型失败把已完成工作
            # 误报成失败；同时在审计数据中留下可解释的降级原因。
            error_code, user_error = classify_model_error(exc)
            return attach_display_result(node, {
                "success": True,
                "content": redact_server_text((result.output or "步骤已完成").strip()),
                "output": result.output,
                "tool": tool_name,
                "attempt": node.retries + 1,
                "method_chain": planned_tools,
                "tool_metadata": result.metadata,
                "step_title": node.name or instruction[:40],
                "presentation_degraded": True,
                "presentation_error": user_error,
                "presentation_error_code": error_code,
            })
        return attach_display_result(node, {
            "success": True,
            "content": redact_server_text((final or result.output or "步骤已完成").strip()),
            "output": result.output,
            "tool": tool_name,
            "attempt": node.retries + 1,
            "method_chain": planned_tools,
            "tool_metadata": result.metadata,
            "step_title": node.name or instruction[:40],
        })
