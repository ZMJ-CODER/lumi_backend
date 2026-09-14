"""规范基础工具协议回归：运行时不保留旧名称转发。"""

import asyncio

from app.agents.skills.executor import execute_tool_call
from app.agents.skills.loader import load_skill_plugins, unload_skill_plugins
from app.agents.skills.registry import ToolRegistry


def _load_base_tools() -> None:
    ToolRegistry.clear()
    unload_skill_plugins()
    load_skill_plugins()


def test_legacy_tool_name_is_rejected_instead_of_silently_rewritten():
    result = asyncio.run(execute_tool_call(
        {"function": {"name": "calculator", "arguments": {"expression": "1+1"}}},
        "u1",
        "office",
    ))

    assert result.success is False
    assert result.error_code == "SKILL_NOT_FOUND"


def test_canonical_tool_name_remains_the_only_protocol_entry():
    _load_base_tools()
    result = asyncio.run(execute_tool_call(
        {"function": {"name": "Calculator", "arguments": {"expression": "1+1"}}},
        "u1",
        "office",
        allow_internal=True,
    ))

    assert result.success is True
    assert result.output == "1+1 = 2"
