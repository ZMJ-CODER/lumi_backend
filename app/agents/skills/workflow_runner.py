"""组合 Skill 运行器。

工作流不拥有工具实例，也不允许直接调用 ``Tool.execute``。本模块是唯一的
组合入口：每一个内部工具步骤重新经过统一执行器，因此权限、审计、确认、
副作用日志和节点互斥锁与模型直接调用完全一致。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.agents.skills.executor import execute_tool_call


ToolInvoker = Callable[[str, dict], Awaitable[ToolOutput]]


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
) -> ToolOutput:
    """执行一个已由编排层选定的组合 Skill。"""

    allowed = set(skill.allowed_tools)

    async def invoke_tool(name: str, arguments: dict) -> ToolOutput:
        result = await execute_tool_call(
            {
                "id": f"workflow-{skill.name}-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            },
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
            execution_scope=context.job_id,
            allowed_tools=allowed,
            allow_internal=True,
        )
        return result

    return await skill.run(params, context, invoke_tool)
