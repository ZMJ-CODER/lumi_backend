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
        # query_knowledge is a Skill-owned service capability, not a public
        # atomic tool.  Keep it available to the developer workflows without
        # re-registering it in the model-visible ToolRegistry.
        if name == "query_knowledge":
            from app.services.rag.knowledge import search_user_knowledge
            from app.core.database import async_session_factory
            from app.services.scene_manager import get_scene_knowledge_tags

            query = str(arguments.get("query") or "").strip()
            if not query or not context.user_id:
                return ToolOutput(success=False, error="知识库检索缺少用户或 query", error_code="INVALID_ARGS")
            try:
                async with async_session_factory() as session:
                    text, citations = await search_user_knowledge(
                        session, user_id=context.user_id, query=query,
                        space_tags=get_scene_knowledge_tags(context.scene),
                        top_k=int(arguments.get("top_k") or 5), exclude_categories=["code"],
                    )
                if not text:
                    return ToolOutput(success=False, error="知识库中未检索到相关内容", error_code="EXEC_ERROR")
                return ToolOutput(success=True, output=text, data={"matches": citations}, metadata={"citations": citations})
            except Exception as exc:  # noqa: BLE001
                return ToolOutput(success=False, error=f"知识库检索失败: {exc}", error_code="EXEC_ERROR", retryable=True)
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
            allow_internal=False,
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

    return await skill.run(params, context, invoke_tool)
