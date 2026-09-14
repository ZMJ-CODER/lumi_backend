"""统一工具输出契约与预算流水线回归。"""

from app.agents.skills.base import SkillResult
from app.agents.skills.output_contract import ToolOutput
from app.services.tool_output_pipeline import normalize_skill_result, render_for_model


def test_legacy_skill_result_is_normalized():
    result = normalize_skill_result(SkillResult(success=True, output="完成"))
    assert isinstance(result, ToolOutput)
    assert result.status == "success"
    assert result.content_type == "text"
    assert result.data == "完成"


def test_structured_output_keeps_delivery_fields_and_budget():
    result = SkillResult(
        success=True,
        data={"answer": "ok", "secret_token": "do-not-show", "items": ["x"]},
        content_type="structured",
    )
    rendered = render_for_model(normalize_skill_result(result), max_chars=80)
    assert "ok" in rendered
    assert "do-not-show" not in rendered
    assert len(rendered) <= 81


def test_artifact_output_only_exposes_reference():
    result = SkillResult(
        success=True,
        data="very large file body",
        content_type="artifact",
        output_meta={"summary": "文件已生成", "artifact_refs": [{"ref_id": "a1", "name": "out.txt"}]},
    )
    rendered = render_for_model(normalize_skill_result(result))
    assert "文件已生成" in rendered
    assert "very large file body" not in rendered
    assert "a1" in rendered


def test_empty_and_partial_are_first_class_statuses():
    empty = normalize_skill_result(SkillResult(success=True, output=""))
    partial = normalize_skill_result(SkillResult(success=True, output="x", metadata={"partial": True}))
    assert empty.status == "empty"
    assert partial.status == "partial"
    assert "不完整" in render_for_model(partial)


def test_legacy_failed_mcp_shape_keeps_error_semantics_in_execution_envelope():
    result = normalize_skill_result({
        "success": True,
        "is_error": True,
        "content": "客户端拒绝执行",
        "error_code": "MCP_EXEC_ERROR",
        "retryable": True,
    })

    assert result.status == "failed"
    assert result.data == "客户端拒绝执行"
    assert result.error == "客户端拒绝执行"
    assert result.error_code == "MCP_EXEC_ERROR"
    assert result.retryable is True


def test_confirmation_result_is_explicitly_pending():
    result = normalize_skill_result(SkillResult(
        success=False,
        error="需要确认",
        error_code="NEEDS_CONFIRMATION",
    ))
    assert result.status == "pending_approval"
    assert result.meta.summary.startswith("[待审批]")


def test_execution_envelope_keeps_call_and_workspace_correlation_fields():
    result = ToolOutput.model_validate({
        "call_id": "call-1", "status": "pending_approval", "data": {"x": 1},
        "content_type": "structured",
        "meta": {"workspace_id": "ws-1", "workspace_version": 4, "transport": "desktop_mcp", "sandbox": True},
    })
    envelope = result.to_execution_envelope()
    assert envelope["call_id"] == "call-1"
    assert envelope["meta"]["workspace_id"] == "ws-1"
    assert envelope["status"] == "pending_approval"
