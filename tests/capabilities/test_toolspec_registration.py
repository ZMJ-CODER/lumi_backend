"""B 项回归：工具注册即声明契约（ToolSpec 影子注册）。

验收标准 #1 的落点：**新工具只靠自己的声明**（``parameters_schema`` /
``result_contract`` / ``write_op`` / ``requires_confirmation``）就能获得输入
Schema、输出契约与治理声明，核心代码无需改动。
"""

from __future__ import annotations

import pytest
from lumi_contracts import RiskLevel, SideEffect, ToolSpec

from app.agents.skills.base import SkillContext, Tool
from app.agents.skills.registry import ToolRegistry
from app.contracts.tools import (
    register_tool_spec,
    tool_spec_from,
    tool_spec_report,
    tool_specs,
)


class _CustomTool(Tool):
    """一个"新工具"：自定义输入 Schema 与输出契约，不改核心代码。"""

    name = "demo_report_builder"
    description = "生成演示报告"
    version = "2.3.1"
    category = "devtools"
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "sections": {"type": "array", "items": {"type": "string"}},
            "dry_run": {"type": "boolean"},
        },
        "required": ["title"],
        "additionalProperties": False,
    }
    result_contract = "lumi.demo_report.result"
    write_op = False

    async def execute(self, params, context: SkillContext | None = None):
        raise NotImplementedError


class _CustomWriteTool(_CustomTool):
    name = "demo_report_publish"
    result_contract = "返回发布后的 URL。"
    write_op = True
    requires_confirmation = True
    permission = "admin"


def test_new_tool_declarations_flow_into_toolspec():
    spec = tool_spec_from(_CustomTool())
    assert isinstance(spec, ToolSpec)
    # 输入 Schema 来自工具自己的声明（不是核心代码里的白名单）
    assert spec.input_schema == _CustomTool.parameters_schema
    # 输出契约名被识别
    assert spec.output_schema_name == "lumi.demo_report.result"
    assert spec.side_effect is SideEffect.NONE
    assert spec.risk_level is RiskLevel.LOW
    assert spec.requires_approval is False
    assert spec.validate_declaration() == []


def test_prose_result_contract_is_not_mistaken_for_a_contract_name():
    spec = tool_spec_from(_CustomWriteTool())
    # "返回发布后的 URL。" 是说明文字，不能冒充契约名
    assert spec.output_schema_name == ""
    assert "返回发布后的 URL。" in spec.description
    # 写工具必须有权限声明 + 审批语义，否则 validate_declaration 会报问题
    assert spec.required_permissions == ("admin",)
    assert spec.requires_approval is True
    assert spec.risk_level is RiskLevel.HIGH
    assert spec.side_effect is SideEffect.EXTERNAL
    assert spec.validate_declaration() == []


def test_incomplete_declaration_is_reported_not_rejected():
    class _BadSchemaTool(_CustomTool):
        name = "demo_bad_write"
        # 参数声明成了非对象 Schema：契约校验必须报出来
        parameters_schema = {"type": "string"}

    problems = register_tool_spec(_BadSchemaTool())
    assert any("input_schema" in item for item in problems)
    # 影子注册：问题被报出来，但工具照常可用（不抛异常）
    assert tool_spec_report()["problems"]["demo_bad_write"]


def test_real_registration_path_shadow_registers_toolspec():
    """真实注册路径（ToolRegistry.register）会同步产出契约 ToolSpec。"""
    before = {spec.name for spec in tool_specs()}
    ToolRegistry.register(_CustomTool(), source="test")
    try:
        after = {spec.name for spec in tool_specs()}
        assert "demo_report_builder" in after - before
        spec = tool_spec_from(ToolRegistry.get("demo_report_builder"))
        assert spec.input_schema == _CustomTool.parameters_schema
        assert spec.version == "2.3.1"
    finally:
        ToolRegistry.unregister("demo_report_builder")


def test_internal_implementations_are_marked_internal():
    class _InternalTool(_CustomTool):
        name = "demo_internal_impl"

    ToolRegistry.register(_InternalTool(), source="test", public=False)
    try:
        spec = tool_spec_from(_InternalTool(), internal=True)
        assert spec.internal is True
        # 内部实现不进模型能力空间，但仍要有完整治理声明
        assert spec.validate_declaration() == []
    finally:
        ToolRegistry.unregister("demo_internal_impl")


def test_toolspec_projection_prefers_declared_projection():
    from lumi_contracts import ExecutionResult, ProjectionKind, Projection

    class _FakeProjection(Projection):
        kind = ProjectionKind.MODEL

        def project(self, result):
            return {"kind": "model", "custom": True, "text": str(result.payload)}

    spec = ToolSpec(name="demo", input_schema={"type": "object"}, model_projection=_FakeProjection())
    view = spec.project("model", ExecutionResult[dict](payload={"a": 1}))
    assert view["custom"] is True and view["text"] == "{'a': 1}"
    # 没声明时退回注册表默认投影（不会因为缺注册而抛错）
    plain = ToolSpec(name="demo2", input_schema={"type": "object"})
    assert "kind" in plain.project("model", ExecutionResult[dict](payload={}))


@pytest.fixture(autouse=True)
def _clean_registry_entries():
    """测试自建工具不得留在真实注册表里。"""
    yield
    for name in ("demo_report_builder", "demo_internal_impl", "demo_bad_write"):
        ToolRegistry.unregister(name)
