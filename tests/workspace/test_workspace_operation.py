"""通用 workspace_operation Skill 的回归测试（只验证声明与 Prompt-as-Code）。"""

from __future__ import annotations

from pathlib import Path


def test_workspace_operation_skill_is_loaded_as_workflow():
    from app.agents.skills.loader import load_skill_plugins
    from app.agents.skills.registry import SkillRegistry

    load_skill_plugins()
    skill = SkillRegistry.get_workflow("workspace_operation")
    assert skill is not None
    assert skill.execution_scope == "backend_orchestrates_client"
    # 允许工具覆盖 读取/暂存写/沙箱/diff/提交/回滚 全链。
    allowed = set(skill.allowed_tools)
    # 读取阶段只声明聚合入口，内部原子读取名不再进入模型工具列表。
    assert "mcp__lumi_client__workspace_navigator" in allowed
    for raw in (
        "workspace_stage_write", "workspace_stage_delete",
        "sandbox_run", "workspace_diff", "workspace_commit", "workspace_rollback",
    ):
        assert f"mcp__lumi_client__{raw}" in allowed
    for leaked in (
        "workspace_list", "workspace_read", "workspace_search",
        "workspace_catalog", "workspace_stat", "workspace_content_extract",
    ):
        assert f"mcp__lumi_client__{leaked}" not in allowed
    # Skill 只声明策略字段；真实审批由 ApprovalPolicyEngine 决定。
    assert skill.approval_policy == "none"
    assert {"RETRIEVE", "ANALYZE", "EXECUTE"}.issubset(set(skill.provided_goals))
    assert "SYSTEM_STATE" in skill.provided_sources


def test_workspace_operation_prompt_describes_single_aggregated_entry():
    prompt = Path("plugins/workflows/prompts/workspace_operation.md").read_text(encoding="utf-8")
    assert "暂存" in prompt
    assert "workspace_diff" in prompt
    assert "workspace_commit" in prompt
    assert "授权策略" in prompt or "系统授权" in prompt
    # Prompt 只描述聚合入口的三个 action，不再直接指示内部原子读取工具。
    assert "workspace_navigator" in prompt
    assert 'action="list"' in prompt and 'action="search"' in prompt and 'action="read"' in prompt
    for legacy in ("workspace_catalog", "workspace_list", "workspace_search", "workspace_content_extract"):
        assert legacy not in prompt


def test_workspace_operation_hides_workspace_id_from_model_schema():
    from importlib.util import module_from_spec, spec_from_file_location

    path = Path("plugins/workflows/developer/workspace_operation.py").resolve()
    spec = spec_from_file_location("workspace_operation_schema_test", path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    definitions = module._tool_defs([type("Capability", (), {
        "to_tool_definition": lambda self: {
            "type": "function",
            "function": {
                "name": "mcp__lumi_client__workspace_stage_write",
                "description": "stage",
                "parameters": {
                    "type": "object",
                    "properties": {"workspace_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["workspace_id", "path", "content"],
                },
            },
        }
    })()])[0]
    assert "workspace_id" not in definitions["function"]["parameters"]["properties"]
    assert "workspace_id" not in definitions["function"]["parameters"]["required"]
