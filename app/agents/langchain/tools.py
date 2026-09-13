"""将现有受控 Tool 适配为 LangChain StructuredTool。

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


def make_tool_search_definition() -> dict:
    """L2 搜索原语定义；它不代表任何业务工具权限。"""
    return {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": "按当前任务描述发现可用工具。只返回少量工具引用；发现后再调用具体工具。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "要完成的任务或能力描述"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def make_domain_discovery_definition() -> dict:
    """L1 domain request primitive; it grants no tool permission by itself."""
    return {
        "type": "function",
        "function": {
            "name": "discover_domain",
            "description": "申请进入一个能力领域（research/document/data/development/system 等）；系统校验权限后才注入域内工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "目标领域"},
                    "reason": {"type": "string", "description": "为什么需要该领域"},
                },
                "required": ["domain"],
                "additionalProperties": False,
            },
        },
    }


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
    authorized_project_ids: tuple[str, ...] | list[str] | None = None,
    authorized_workspace_id: str = "",
    execution_scope: str = "",
    allowed_tools: set[str] | None = None,
    display_name: str = "",
) -> StructuredTool | None:
    """构造绑定到当前用户/场景的工具实例，不能跨用户复用。

    ``display_name``（方案《资源能力层》Phase 5）：模型可见工具面收敛后，模型看到的
    是 ``Read/Write/Edit/…`` 这类稳定名，而实现仍然是原来的能力。**模型传下来的名字
    就是对外名**（审计与错误信息都以它为准），执行器用
    ``executor._resolve_model_alias`` 解析回实现名——一处解析，处处一致。
    """
    capability = await get_tool_capability(name, scene, user_role, user_id)
    if capability is None:
        return None
    # 模型传下来的名字 = 对外名（收敛关闭时就是实现名）。执行器负责把它解析回实现名
    # （``_resolve_model_alias``），因此审计里能同时看到"模型叫了什么"和"实际跑了什么"。
    model_name = str(display_name or capability.name)

    async def invoke_skill(**kwargs: Any) -> str:
        result = await execute_tool_call(
            {
                "id": f"langchain-{name}",
                "type": "function",
                "function": {"name": model_name, "arguments": json.dumps(kwargs, ensure_ascii=False)},
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
            authorized_project_ids=authorized_project_ids,
            authorized_workspace_id=authorized_workspace_id,
            execution_scope=execution_scope,
            allowed_tools=allowed_tools,
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
        name=model_name,
        description=capability.description,
        args_schema=_safe_schema(capability.parameters),
    )
