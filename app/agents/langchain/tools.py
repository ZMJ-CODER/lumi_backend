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


def _safe_schema(schema: dict | None) -> dict:
    schema = schema if isinstance(schema, dict) else {}
    return {
        "type": "object",
        "properties": dict(schema.get("properties") or {}),
        "required": list(schema.get("required") or []),
    }


async def make_skill_tool(
    name: str,
    *,
    user_id: str,
    scene: str,
    conversation_id: str = "",
    user_role: str = "user",
    on_notify: Callable[[str | dict], None] | None = None,
    on_result: Callable[[SkillResult], Any] | None = None,
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
        )
        # ToolNode 会把抛出的异常转成一条工具消息，但那会中断我们的审计/引用
        # 收集，也会把底层异常文本暴露给模型。失败统一作为受控工具结果回填，
        # 由下一轮模型决定修正参数、换方法或直接说明限制。
        if on_result:
            maybe_awaitable = on_result(result)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        if result.success:
            return result.output or "步骤已完成"
        return f"工具未完成（{result.error_code or 'EXEC_ERROR'}）：{result.error or '执行失败'}"

    return StructuredTool.from_function(
        coroutine=invoke_skill,
        name=capability.name,
        description=capability.description,
        args_schema=_safe_schema(capability.parameters),
    )
