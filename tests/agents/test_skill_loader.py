"""技能插件加载器测试：扫描/注册/热更新/覆盖恢复."""

import asyncio

import pytest

from app.agents.skills import loader
from app.agents.skills.registry import SkillRegistry, ToolRegistry
from app.core.config import settings


@pytest.fixture(autouse=True)
def _skills():
    """加载真实插件目录，测试结束后清理."""
    SkillRegistry.clear()
    loader.unload_skill_plugins()
    loader.load_skill_plugins()
    yield
    loader.unload_skill_plugins()
    SkillRegistry.clear()


def _write_tool_plugin(plugin_dir, name, output):
    (plugin_dir / f"{name}.py").write_text(
        f"""
from app.agents.skills.base import Tool, SkillResult

class {name.title()}Tool(Tool):
    name = "{name}"
    description = "test plugin {name}"
    category = "computation"
    scenes = ["chat"]

    async def execute(self, params, context=None):
        return SkillResult(success=True, output="{output}")
""",
        encoding="utf-8",
    )


def _plugin_skill_output(skill):
    return asyncio.run(skill.execute({})).output


def test_plugin_load_reload_unload(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "skills"
    plugin_dir.mkdir()
    monkeypatch.setattr(settings, "TOOL_PLUGINS_DIR", str(plugin_dir))
    monkeypatch.setattr(settings, "WORKFLOW_SKILLS_DIR", str(tmp_path / "workflows"))
    loader.unload_skill_plugins()

    _write_tool_plugin(plugin_dir, "hello", "v1")
    assert loader.load_skill_plugins() == 1
    skill = ToolRegistry.get("hello")
    assert skill is not None
    assert ToolRegistry.get_source("hello") == "plugin"
    assert _plugin_skill_output(skill) == "v1"

    # 热更新：改文件 → reload → 新逻辑生效（不重启）
    _write_tool_plugin(plugin_dir, "hello", "v2")
    result = loader.reload_skill_plugins()
    assert result["registered"] == 1
    assert _plugin_skill_output(ToolRegistry.get("hello")) == "v2"

    # 卸载后移除
    loader.unload_skill_plugins()
    assert ToolRegistry.get("hello") is None


def test_loader_rejects_a_workflow_in_the_tools_directory(tmp_path, monkeypatch):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    (tool_dir / "misplaced.py").write_text(
        """
from app.agents.skills.base import WorkflowSkill

class MisplacedWorkflow(WorkflowSkill):
    name = "misplaced_workflow"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "TOOL_PLUGINS_DIR", str(tool_dir))
    monkeypatch.setattr(settings, "WORKFLOW_SKILLS_DIR", str(tmp_path / "workflows"))
    loader.unload_skill_plugins()

    assert loader.load_skill_plugins() == 0
    assert ToolRegistry.get("misplaced_workflow") is None
    assert SkillRegistry.get_workflow("misplaced_workflow") is None


def test_loader_rejects_a_tool_in_the_workflows_directory(tmp_path, monkeypatch):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "misplaced.py").write_text(
        """
from app.agents.skills.base import Tool, SkillResult

class MisplacedTool(Tool):
    name = "misplaced_tool"
    description = "misplaced"
    category = "system"
    domain = "system"
    use_when = ["test"]
    do_not_use_when = ["test"]

    async def execute(self, params, context=None):
        return SkillResult(success=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "TOOL_PLUGINS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(settings, "WORKFLOW_SKILLS_DIR", str(workflow_dir))
    loader.unload_skill_plugins()

    assert loader.load_skill_plugins() == 0
    assert ToolRegistry.get("misplaced_tool") is None
    assert SkillRegistry.get_workflow("misplaced_tool") is None
