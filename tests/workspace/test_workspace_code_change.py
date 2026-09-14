from __future__ import annotations


from app.agents.skills.base import SkillContext
from app.agents.skills.registry import SkillRegistry


def test_workspace_code_change_skill_is_loaded_as_workflow():
    from app.agents.skills.loader import load_skill_plugins

    load_skill_plugins()
    skill = SkillRegistry.get_workflow("workspace_code_change")
    assert skill is not None
    assert skill.execution_scope == "backend_orchestrates_client"
    assert skill.approval_policy == "before_submit"
    assert "mcp__lumi_client__workspace_navigator" in skill.allowed_tools
    assert "mcp__lumi_client__workspace_read" not in skill.allowed_tools
    assert "mcp__lumi_client__workspace_list" not in skill.allowed_tools


def test_workspace_context_is_explicit_and_not_model_selectable():
    context = SkillContext(user_id="u1", workspace_id="ws-trusted")
    assert context.workspace_id == "ws-trusted"


def test_workspace_skill_hides_workspace_id_from_model_schema():
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = Path("plugins/workflows/developer/workspace_code_change.py").resolve()
    spec = spec_from_file_location("workspace_code_change_test", path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    schema = module._tool_defs([type("Capability", (), {
        "to_tool_definition": lambda self: {
            "type": "function",
            "function": {
                "name": "mcp__lumi_client__workspace_read",
                "description": "read",
                "parameters": {
                    "type": "object",
                    "properties": {"workspace_id": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["workspace_id", "path"],
                },
            },
        }
    })()])[0]
    assert "workspace_id" not in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["required"] == ["path"]


def test_workspace_code_change_covers_read_before_execute():
    from app.agents.skills.loader import load_skill_plugins

    load_skill_plugins()
    skill = SkillRegistry.get_workflow("workspace_code_change")
    assert skill is not None
    assert {"RETRIEVE", "ANALYZE", "EXECUTE"}.issubset(set(skill.provided_goals))
    assert "SYSTEM_STATE" in skill.provided_sources
    assert "USER_INPUT" in skill.provided_sources


def test_workspace_code_change_prompt_treats_empty_directory_as_success():
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = Path("plugins/workflows/developer/workspace_code_change.py").resolve()
    spec = spec_from_file_location("workspace_code_change_prompt_test", path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    prompt = Path("plugins/workflows/prompts/workspace_code_change.md").read_text(encoding="utf-8")
    assert "空目录" in prompt or "空文件夹" in prompt
