"""组合 Skill 运行器。

工作流不拥有工具实例，也不允许直接调用 ``Tool.execute``。本模块是唯一的
组合入口：每一个内部工具步骤重新经过统一执行器，因此权限、审计、确认、
副作用日志和节点互斥锁与模型直接调用完全一致。
"""

from __future__ import annotations

import json
import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.agents.skills.executor import execute_tool_call
from app.core.config import settings
from lumi_contracts import SkillStep, skill_step_from_tool_output


ToolInvoker = Callable[[str, dict], Awaitable[ToolOutput]]


def _monotonic() -> float:
    """Local clock seam; tests must not replace the process-wide time module."""
    return time.monotonic()


async def run_workflow_skill(
    skill: WorkflowSkill,
    params: dict,
    context: SkillContext,
    *,
    user_role: str = "user",
    user_message: str = "",
    confirmed_tools: frozenset[str] | set[str] | None = None,
    confirmed_tool_calls: frozenset[str] | set[str] | None = None,
    approval_context_sha256: str = "",
    authorized_project_ids: tuple[str, ...] = (),
    workspace_id: str = "",
) -> ToolOutput:
    """执行一个已由编排层选定的组合 Skill（并对步骤做契约级记账）。"""
    from app.contracts.skill_result import skill_result_to_tool_output, to_skill_result

    steps: list[SkillStep] = []
    result = await _run_workflow_skill(
        skill,
        params,
        context,
        steps=steps,
        user_role=user_role,
        user_message=user_message,
        confirmed_tools=confirmed_tools,
        confirmed_tool_calls=confirmed_tool_calls,
        approval_context_sha256=approval_context_sha256,
        authorized_project_ids=authorized_project_ids,
        workspace_id=workspace_id,
    )
    # SkillResult：Skill 级控制信息 + 步骤账；回程仍是旧 ToolOutput（步骤账进
    # quality_hints），下游既有消费者无需改动。
    skill_result = to_skill_result(result, skill_name=skill.name, steps=steps)
    return skill_result_to_tool_output(skill_result, base=result)


async def _run_workflow_skill(
    skill: WorkflowSkill,
    params: dict,
    context: SkillContext,
    *,
    steps: list[SkillStep],
    user_role: str = "user",
    user_message: str = "",
    confirmed_tools: frozenset[str] | set[str] | None = None,
    confirmed_tool_calls: frozenset[str] | set[str] | None = None,
    approval_context_sha256: str = "",
    authorized_project_ids: tuple[str, ...] = (),
    workspace_id: str = "",
) -> ToolOutput:
    """组合 Skill 的实际执行体（每次工具调用都会记一条 ``SkillStep``）。"""

    allowed = set(skill.allowed_tools)
    allowed.update(
        str(item.get("name") or "")
        for item in skill.effective_dependencies().get("tools", [])
        if isinstance(item, dict) and str(item.get("name") or "")
    )

    client_approval_wait_s = max(5.0, float(settings.MCP_CLIENT_APPROVAL_WAIT_S))

    async def invoke_tool(name: str, arguments: dict) -> ToolOutput:
        """记录步骤账的薄包装：每个内部工具调用恰好产生一条 ``SkillStep``。"""
        started = _monotonic()
        result = await _invoke_tool(name, arguments)
        steps.append(
            skill_step_from_tool_output(
                result, name=name, index=len(steps), tool=name
            ).model_copy(update={"duration_ms": int((_monotonic() - started) * 1000)})
        )
        return result

    async def _invoke_tool(name: str, arguments: dict) -> ToolOutput:
        # Workspace IDs are a server-side authority, not a model-selectable
        # parameter.  A workflow may omit it; it may never switch it.
        target_args = dict(arguments or {})
        raw_name = name.split("__", 2)[-1] if name.startswith("mcp__") else name
        if raw_name.startswith(("workspace_", "sandbox_")):
            if not workspace_id:
                return ToolOutput(
                    success=False,
                    error="当前任务没有已选择的工作区；请先在办公模式中新建或打开项目",
                    error_code="WORKSPACE_SCOPE_REQUIRED",
                    retryable=False,
                )
            supplied = str(target_args.get("workspace_id") or "").strip()
            if supplied and supplied != workspace_id:
                return ToolOutput(
                    success=False,
                    error="工具请求的工作区不属于当前任务",
                    error_code="WORKSPACE_SCOPE_FORBIDDEN",
                    retryable=False,
                )
            target_args["workspace_id"] = workspace_id
        # query_knowledge is a Skill-owned service capability, not a public
        # atomic tool.  Keep it available to the developer workflows without
        # re-registering it in the model-visible ToolRegistry.
        if name == "query_knowledge":
            from app.knowledge.api import search_user_knowledge
            from app.core.database import async_session_factory
            from app.services.scene_manager import get_scene_knowledge_tags

            query = str(target_args.get("query") or "").strip()
            if not query or not context.user_id:
                return ToolOutput(success=False, error="知识库检索缺少用户或 query", error_code="INVALID_ARGS")
            try:
                async with async_session_factory() as session:
                    text, citations = await search_user_knowledge(
                        session, user_id=context.user_id, query=query,
                        space_tags=get_scene_knowledge_tags(context.scene),
                        top_k=int(target_args.get("top_k") or 5), exclude_categories=["code"],
                    )
                if not text:
                    return ToolOutput(success=False, error="知识库中未检索到相关内容", error_code="EXEC_ERROR")
                return ToolOutput(success=True, output=text, data={"matches": citations}, metadata={"citations": citations})
            except Exception as exc:  # noqa: BLE001
                return ToolOutput(success=False, error=f"知识库检索失败: {exc}", error_code="EXEC_ERROR", retryable=True)
        # One logical client action keeps one call id across local approval.
        # Electron records the decision/result under this id; changing it on
        # retry would open a second dialog and could execute a commit twice.
        mcp_call_id = str(uuid.uuid4())
        tool_call = {
            "id": mcp_call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(target_args, ensure_ascii=False)},
        }

        async def execute_once() -> ToolOutput:
            return await execute_tool_call(
                tool_call,
                context.user_id,
                context.scene,
                context.conversation_id or context.job_id,
                on_notify=context.on_notify,
                on_output=context.on_output,
                user_role=user_role,
                user_message=user_message,
                llm_api_key=context.llm_api_key,
                llm_config=context.llm_config,
                confirmed_tools=confirmed_tools,
                confirmed_tool_calls=confirmed_tool_calls,
                approval_context_sha256=approval_context_sha256,
                office_doc_ids=context.office_doc_ids,
                authorized_project_ids=authorized_project_ids or context.authorized_project_ids,
                authorized_workspace_id=workspace_id or context.workspace_id,
                execution_scope=context.job_id,
                allowed_tools=allowed,
                allow_internal=False,
                mcp_call_id=mcp_call_id,
            )

        result = await execute_once()
        raw_name = name.split("__", 2)[-1] if name.startswith("mcp__") else name
        client_workspace_call = name.startswith("mcp__") and raw_name.startswith(("workspace_", "sandbox_"))
        if result.status == "pending_approval" and client_workspace_call:
            if context.on_notify:
                notice = "变更已准备，正在等待你在客户端确认…"
                maybe = context.on_notify(notice)
                if hasattr(maybe, "__await__"):
                    await maybe
            deadline = _monotonic() + client_approval_wait_s
            poll_seconds = 1.0
            # 轮询次数上限：时钟被测试桩/系统单调时钟异常卡住时也不能死循环。
            max_polls = max(1, int(client_approval_wait_s))
            polls = 0
            while (
                result.status == "pending_approval"
                and _monotonic() < deadline
                and polls < max_polls
            ):
                await asyncio.sleep(poll_seconds)
                result = await execute_once()
                polls += 1
                poll_seconds = min(3.0, poll_seconds + 0.5)
            if result.status == "pending_approval":
                return ToolOutput(
                    status="cancelled",
                    call_id=mcp_call_id,
                    data=result.data,
                    error="等待客户端确认超时，本次操作尚未执行",
                    error_code="CLIENT_APPROVAL_TIMEOUT",
                    retryable=False,
                    meta=result.meta,
                )
        # The unified execution envelope carries structured tool payloads in
        # ``data``.  Normalize the text projection here as a second defensive
        # boundary so Workflow implementations can consume search/fetch
        # material regardless of whether the call crossed MCP.
        if result.success and not str(result.output or "").strip() and isinstance(result.data, dict):
            for key in ("summary", "answer", "result", "text", "content"):
                value = result.data.get(key)
                if isinstance(value, str) and value.strip():
                    result = result.model_copy(update={"output": value.strip()})
                    break
            else:
                sources = result.data.get("sources")
                if isinstance(sources, list):
                    lines = []
                    for index, item in enumerate(sources[:10], 1):
                        if not isinstance(item, dict):
                            continue
                        lines.append(
                            f"[{index}] {item.get('title') or '未命名来源'}\n"
                            f"{item.get('url') or item.get('source') or ''}\n"
                            f"{item.get('snippet') or item.get('summary') or item.get('content') or ''}"
                        )
                    if lines:
                        result = result.model_copy(update={"output": "\n\n".join(lines)[:12000]})
        return result

    from app.agents.skills.dependencies import resolve_dependencies, synthesize_aggregated_capabilities
    from app.agents.skills.executor import get_capabilities_for_scene, get_desktop_mcp_capabilities

    capability_rows = await get_capabilities_for_scene(
        context.scene, user_role, context.user_id, include_internal=True
    )
    capability_rows.extend(
        await get_desktop_mcp_capabilities(context.user_id, context.scene, user_role)
    )
    capability_map = {
        item.name: {
            "version": item.version,
            "provider": str((item.annotations or {}).get("provider") or ("external_mcp" if item.source == "mcp" else item.environment)),
            "environment": item.environment,
            "annotations": dict(item.annotations or {}),
        }
        for item in capability_rows
    }
    # 聚合读取入口由后端合成（Electron 只发布内部原子工具），否则依赖解析会把
    # workspace_navigator 误判为 MISSING_TOOL。
    for name, row in synthesize_aggregated_capabilities(capability_map).items():
        capability_map.setdefault(name, row)
    dependency_report = resolve_dependencies(
        skill.effective_dependencies(), capability_map,
        execution_scope=str(getattr(skill, "execution_scope", "backend") or "backend"),
    )
    if dependency_report.required_issues:
        first = dependency_report.required_issues[0]
        return ToolOutput(
            success=False,
            error=first.message,
            error_code=f"WORKFLOW_DEPENDENCY_{first.code}",
            retryable=first.code in {"CLIENT_OFFLINE", "PROVIDER_UNAVAILABLE"},
            metadata={
                "skill": skill.name,
                "fallback_policy": str(getattr(skill, "fallback_policy", "clarify") or "clarify"),
                "dependency_report": dependency_report.as_dict(),
            },
        )

    return await skill.run(params, context, invoke_tool)


__all__ = ["ToolInvoker", "run_workflow_skill"]
