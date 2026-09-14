"""用户私有 Workflow Skill 的隔离与声明式安全边界测试。"""

from __future__ import annotations

import uuid

import pytest

from app.agents.skills.base import Tool, ToolOutput
from app.agents.skills.registry import SkillRegistry, ToolRegistry
from app.services.user_workflow_skills import (
    _validate_definition,
    validate_input_schema,
    validate_user_skill_name,
)


class _ReadOnlyTool(Tool):
    name = "user_workflow_read_tool"
    description = "测试只读工具"
    category = "system"
    domain = "system"
    use_when = ["测试声明式流程"]
    do_not_use_when = ["测试写操作"]
    user_workflow_allowed = True
    parameters_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, params: dict, context=None) -> ToolOutput:
        return ToolOutput(success=True, output=str(params.get("query") or ""))


class _WriteTool(_ReadOnlyTool):
    name = "user_workflow_write_tool"
    write_op = True
    user_workflow_allowed = True


@pytest.fixture(autouse=True)
def _isolated_registry():
    SkillRegistry.clear()
    ToolRegistry.clear()
    yield
    SkillRegistry.clear()
    ToolRegistry.clear()


def test_private_skills_are_visible_only_to_the_owner_and_override_public_name():
    from app.services.user_workflow_skills import DeclarativeUserWorkflowSkill

    owner = str(uuid.uuid4())
    other = str(uuid.uuid4())
    from app.agents.skills.base import WorkflowSkill

    class PublicFlow(WorkflowSkill):
        name = "shared_flow"

    SkillRegistry.register(PublicFlow())
    private = DeclarativeUserWorkflowSkill(type("Record", (), {
        "name": "shared_flow", "description": "私有", "version": 1, "status": "enabled",
        "category": "user", "scenes": ["office"], "allowed_tools": [], "input_schema": {},
        "steps": [], "user_id": owner,
    })())
    SkillRegistry.register(private, source="user")

    assert SkillRegistry.get_visible_workflow("shared_flow", owner) is private
    assert SkillRegistry.get_visible_workflow("shared_flow", other).description == ""
    assert [item.name for item in SkillRegistry.list_visible(other)] == ["shared_flow"]


def test_user_workflow_can_only_reference_explicitly_allowed_read_tools():
    ToolRegistry.register(_ReadOnlyTool())
    ToolRegistry.register(_WriteTool())
    _validate_definition(["user_workflow_read_tool"], [{"tool": "user_workflow_read_tool", "arguments": {"query": "x"}}])

    with pytest.raises(ValueError, match="未注册或不可声明"):
        _validate_definition(["user_workflow_write_tool"], [{"tool": "user_workflow_write_tool", "arguments": {}}])
    with pytest.raises(ValueError, match="步骤工具未在 allowed_tools"):
        _validate_definition(["user_workflow_read_tool"], [{"tool": "other", "arguments": {}}])
    with pytest.raises(ValueError, match="脚本、命令或 URL"):
        _validate_definition(["user_workflow_read_tool"], [{"tool": "user_workflow_read_tool", "arguments": {"query": "https://bad.example"}}])
    with pytest.raises(ValueError, match="未声明的输入"):
        _validate_definition(
            ["user_workflow_read_tool"],
            [{"tool": "user_workflow_read_tool", "arguments": {"query": "{{missing}}"}}],
            input_names={"topic"},
        )


def test_user_skill_name_cannot_shadow_tool_or_public_workflow():
    ToolRegistry.register(_ReadOnlyTool())
    with pytest.raises(ValueError, match="Tool 重名"):
        validate_user_skill_name("user_workflow_read_tool")

    from app.agents.skills.base import WorkflowSkill

    class PublicFlow(WorkflowSkill):
        name = "public_flow"

    SkillRegistry.register(PublicFlow())
    with pytest.raises(ValueError, match="公共 Skill 重名"):
        validate_user_skill_name("public_flow")


def test_user_input_schema_rejects_url_and_non_identifier_fields():
    validate_input_schema({"properties": {"topic": {"type": "string"}}})
    with pytest.raises(ValueError, match="URL"):
        validate_input_schema({"properties": {"url": {"type": "string", "format": "uri"}}})
    with pytest.raises(ValueError, match="合法标识符"):
        validate_input_schema({"properties": {"not-valid": {"type": "string"}}})


def test_user_workflow_resolves_only_explicit_input_placeholders_recursively():
    from app.services.user_workflow_skills import DeclarativeUserWorkflowSkill

    owner = str(uuid.uuid4())
    skill = DeclarativeUserWorkflowSkill(type("Record", (), {
        "name": "nested_input_flow", "description": "私有", "version": 1, "status": "enabled",
        "category": "user", "scenes": ["office"], "allowed_tools": ["user_workflow_read_tool"],
        "input_schema": {}, "user_id": owner,
        "steps": [{"tool": "user_workflow_read_tool", "arguments": {"query": "{{topic}}", "nested": ["{{topic}}"]}}],
    })())

    calls = []

    async def invoke(name, arguments):
        calls.append((name, arguments))
        return ToolOutput(success=True, output="ok")

    from app.agents.skills.base import SkillContext

    import asyncio

    result = asyncio.run(skill.run({"topic": "预算"}, SkillContext(user_id=owner), invoke))
    assert result.success is True
    assert calls == [("user_workflow_read_tool", {"query": "预算", "nested": ["预算"]})]


def test_compiler_rejects_workflow_not_visible_to_the_requesting_user(monkeypatch):
    import asyncio

    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.plan_compiler import compile_plan

    async def no_visible_workflows(_user_id):
        return []

    monkeypatch.setattr("app.services.user_workflow_skills.get_visible_workflow_skills", no_visible_workflows)
    compiled = asyncio.run(compile_plan(
        [TaskNode(id="flow", name="私有流程", agent="workflow_skill", params={"skill_name": "other_users_flow", "inputs": {}})],
        scene="office", user_role="user", user_id=str(uuid.uuid4()), workers={"workflow_skill": object()},
    ))
    assert compiled.ok is False
    assert any(item.code == "WORKFLOW_UNAVAILABLE" for item in compiled.violations)
