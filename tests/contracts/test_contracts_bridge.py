"""迁移桥接层契约测试：``app.contracts`` 与 ``lumi_contracts`` 必须一致。"""

from __future__ import annotations

import pytest

from app.contracts import (
    ExecutionResult,
    ExecutionStatus,
    ProjectionKind,
    ToolOutput,
    adapt_tool_result,
    model_text,
    project,
    project_all,
    to_execution_result,
)
from app.agents.skills.output_contract import ToolOutput as LegacyToolOutput


def test_legacy_tool_output_is_reexported_and_adapted():
    legacy = LegacyToolOutput(status="success", data={"ok": True})
    assert isinstance(legacy, ToolOutput)
    converted = to_execution_result(legacy, tool_name="demo")
    assert isinstance(converted, ExecutionResult)
    assert converted.status is ExecutionStatus.SUCCESS
    assert converted.payload == {"ok": True}


def test_projection_helpers_produce_four_views():
    result = to_execution_result(
        LegacyToolOutput(
            status="success",
            data={
                "status": "ok",
                "summary": "已读取 a.txt",
                "sections": [{"source": "a.txt", "location": "line-1", "text": "正文事实"}],
            },
        ),
        tool_name="workspace_navigator",
    )
    views = project_all(result)
    assert set(views) == {"model", "ui", "audit", "storage"}
    assert "正文事实" in model_text(result)
    assert project(result, "ui")["summary"] == "已读取 a.txt"
    assert project(result, ProjectionKind.AUDIT)["tool"] == "workspace_navigator"


def test_unknown_content_type_still_raises_through_bridge():
    from app.contracts import UnsupportedContractVersion

    with pytest.raises(UnsupportedContractVersion):
        adapt_tool_result({"status": "success", "data": {}, "content_type": "weird"})


def test_contract_exports_are_complete():
    """``app.contracts`` 必须再导出契约包的全部公开名字（避免迁移期出现盲区）。"""
    import lumi_contracts

    missing = [name for name in lumi_contracts.__all__ if not hasattr(lumi_contracts, name)]
    assert missing == []
    from app import contracts

    for name in ("ExecutionResult", "ToolSpec", "StreamEvent", "JobRunView", "ToolRegistry"):
        assert hasattr(contracts, name), f"app.contracts 缺少再导出：{name}"
