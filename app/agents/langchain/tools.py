"""将现有受控 Skill 适配为 LangChain StructuredTool。

这里不把权限判断交给模型：工具暴露前仍经过 scene/role/runtime 过滤，调用时
仍回到 execute_tool_call 的审计、参数校验、用户隔离和确认逻辑。
"""

from __future__ import annotations

import json
import inspect
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from app.agents.skills.base import SkillResult
from app.agents.skills.executor import execute_tool_call, get_tool_capability
from app.services.tool_output_projection import project_tool_output


def _safe_schema(schema: dict | None) -> dict:
    schema = schema if isinstance(schema, dict) else {}
    return {
        "type": "object",
        "properties": dict(schema.get("properties") or {}),
        "required": list(schema.get("required") or []),
    }


def _tool_result_for_model(result: SkillResult) -> str:
    """Return data plus concise next-step signals, never server internals."""
    return project_tool_output(result)


async def make_skill_tool(
    name: str,
    *,
    user_id: str,
    scene: str,
    conversation_id: str = "",
    user_role: str = "user",
    llm_config: dict | None = None,
    on_notify: Callable[[str | dict], None] | None = None,
    on_result: Callable[[SkillResult], Any] | None = None,
    user_message: str = "",
    approval_context_sha256: str = "",
    office_doc_ids: tuple[str, ...] | list[str] | None = None,
    execution_scope: str = "",
) -> StructuredTool | None:
    """构造绑定到当前用户/场景的工具实例，不能跨用户复用。"""
    capability = await get_tool_capability(name, scene, user_role)
    if capability is None:
        return None

    async def invoke_skill(**kwargs: Any) -> str:
        result = await execute_tool_call(
            {
                "id": f"langchain-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(kwargs, ensure_ascii=False)},
            },
            user_id,
            scene,
            conversation_id,
            on_notify=on_notify,
            user_role=user_role,
            user_message=user_message,
            llm_config=llm_config,
            approval_context_sha256=approval_context_sha256,
            office_doc_ids=office_doc_ids,
            execution_scope=execution_scope,
        )
        # ToolNode 会把抛出的异常转成一条工具消息，但那会中断我们的审计/引用
        # 收集，也会把底层异常文本暴露给模型。失败统一作为受控工具结果回填，
        # 由下一轮模型决定修正参数、换方法或直接说明限制。
        if on_result:
            maybe_awaitable = on_result(result)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return _tool_result_for_model(result)

    return StructuredTool.from_function(
        coroutine=invoke_skill,
        name=capability.name,
        description=capability.description,
        args_schema=_safe_schema(capability.parameters),
    )
