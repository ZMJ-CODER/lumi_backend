"""基础工具目录与白名单回归测试。"""

import asyncio

from app.agents.skills.discovery import base_tool_names
from app.agents.skills.executor import get_capabilities_for_scene
from app.agents.skills.loader import load_skill_plugins, unload_skill_plugins
from app.agents.skills.registry import ToolRegistry
from app.core.config import settings


BASE_NAMES = {
    "Task", "Bash", "Glob", "Grep", "ExitPlanMode", "Read", "Edit", "Write",
    "NotebookEdit", "web_fetch", "TodoWrite", "web_search", "BashOutput", "KillShell",
    "AskUserQuestion", "Skill", "SlashCommand", "EnterPlanMode",
}


def test_base_policy_contains_exactly_the_canonical_tools():
    assert base_tool_names() == BASE_NAMES


def test_base_tools_only_filters_function_calling_namespace():
    previous = settings.AGENT_BASE_TOOLS_ONLY
    try:
        unload_skill_plugins()
        load_skill_plugins()
        settings.AGENT_BASE_TOOLS_ONLY = True
        names = {item.name for item in asyncio.run(get_capabilities_for_scene("office"))}
        assert names <= BASE_NAMES
        assert names == BASE_NAMES
    finally:
        settings.AGENT_BASE_TOOLS_ONLY = previous
        unload_skill_plugins()


def test_redundant_devtools_are_not_registered():
    """已由 Bash/Skill 流程覆盖的旧命令探测工具不应回到候选池。"""
    assert ToolRegistry.get("lint_code") is None
    assert ToolRegistry.get("run_tests") is None
