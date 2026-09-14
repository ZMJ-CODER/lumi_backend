"""契约包边界测试（方案验收 1/3/4/7/8）。

覆盖：

* 新工具可自定义输入/输出，无需改核心；
* 旧 ``ToolOutput`` / MCP 裸字典经 Adapter 无损转换，裸字典不扩散；
* 任意结果都能产出模型/UI/审计/持久化四种投影；
* 工作区读取结果：正文进 payload/artifact，模型投影有预算且路径不泄漏绝对目录；
* 事件版本 + 递增序号，未知事件类型可被识别但不致命；
* 无法转换的契约版本必须显式报 UNSUPPORTED_CONTRACT_VERSION，不静默丢字段。
"""

from __future__ import annotations

import pytest

from lumi_contracts import (
    ContractError,
    ContractErrorCode,
    ContractVersion,
    ExecutionResult,
    ExecutionStatus,
    EventSequencer,
    ProjectionKind,
    RunState,
    Sensitivity,
    ServerContext,
    SideEffect,
    StreamEventType,
    StreamEvent,
    ToolSpec,
    UnsupportedContractVersion,
    adapt_tool_result,
    approval_fingerprint,
    can_transition,
    default_projection_registry,
    default_tool_registry,
    failure,
    ok,
)


# ── 1. 新工具：自定义输入/输出，核心零改动 ──────────────────

class FakePayload:
    """第三方风格的结果对象（既不是 pydantic 也不是 dataclass）。"""

    def __init__(self) -> None:
        self.total_hits = 7
        self.items = [{"path": "a.py"}, {"path": "b.py"}]


def test_new_tool_with_custom_io_registers_without_core_changes():
    def _executor(arguments, context):  # pragma: no cover - 仅证明可注入
        return arguments

    spec = ToolSpec(
        name="demo_search",
        namespace="lumi",
        description="演示：自定义输入输出",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["query"],
        },
        output_schema_name="lumi.demo_search.result",
        output_schema_version=1,
        executor=_executor,
        side_effect=SideEffect.NONE,
        required_permissions=("workspace:read",),
    )
    registry = default_tool_registry()
    registry.register(spec, replace=True)
    found = registry.require("demo_search", "lumi")
    assert found.input_schema["properties"]["query"]["type"] == "string"
    assert found.qualified_name == "lumi.demo_search"

    # 业务 payload 自由定义，直接按属性访问，没有 canonical_data 中间层
    result: ExecutionResult[FakePayload] = ok(
        FakePayload(), tool_name="lumi.demo_search", schema_name="lumi.demo_search.result"
    )
    assert result.payload is not None and result.payload.total_hits == 7
    registry.unregister("demo_search", "lumi")


def test_tool_spec_declaration_is_enforced():
    """有副作用却不声明权限/审批的高风险工具必须被拒绝。"""
    bad = ToolSpec(
        name="danger",
        input_schema={"type": "object", "properties": {}},
        side_effect=SideEffect.WORKSPACE_WRITE,
    )
    problems = bad.validate_declaration()
    assert any("required_permissions" in item for item in problems)
    with pytest.raises(ContractError) as exc:
        default_tool_registry().register(bad)
    assert exc.value.code == ContractErrorCode.INVALID_CONTRACT


def test_untrusted_namespace_is_rejected():
    spec = ToolSpec(
        name="third_party",
        namespace="random_vendor",
        input_schema={"type": "object", "properties": {}},
    )
    with pytest.raises(ContractError) as exc:
        default_tool_registry().register(spec)
    assert exc.value.code == ContractErrorCode.PERMISSION_DENIED


def test_credential_tool_cannot_allow_network():
    spec = ToolSpec(
        name="dump_secrets",
        namespace="lumi",
        input_schema={"type": "object", "properties": {}},
        data_sensitivity=Sensitivity.CREDENTIAL,
        allow_network=True,
    )
    assert any("allow_network" in item for item in spec.validate_declaration())


# ── 2. 旧结果适配（裸字典只在 Adapter 内部） ────────────────

def test_legacy_tool_output_is_adapted_without_loss():
    from app.agents.skills.output_contract import ArtifactRef as LegacyArtifact
    from app.agents.skills.output_contract import OutputMeta, ToolOutput

    legacy = ToolOutput(
        status="partial",
        call_id="c1",
        data={"entries": [{"path": "a.txt"}]},
        content_type="structured",
        meta=OutputMeta(
            total_size=12,
            summary="已读取 a.txt",
            artifact_refs=[LegacyArtifact(ref_id="r1", name="a.txt", size=12)],
        ),
        metadata={"decision_signals": {"result_count": 1}},
    )
    adapted = adapt_tool_result(legacy, tool_name="workspace_navigator")
    assert adapted.status is ExecutionStatus.PARTIAL
    assert adapted.call_id == "c1"
    assert adapted.content_type == "structured"
    assert adapted.payload == {"entries": [{"path": "a.txt"}]}
    assert adapted.artifact_refs[0].ref_id == "r1"
    assert adapted.metadata["summary"] == "已读取 a.txt"
    assert adapted.metadata["decision_signals"]["result_count"] == 1


def test_legacy_mcp_envelope_dict_is_adapted():
    envelope = {
        "call_id": "c9",
        "status": "failed",
        "data": "读取失败",
        "content_type": "text",
        "error": "设备离线",
        "error_code": "WORKSPACE_DEVICE_OFFLINE",
        "retryable": True,
    }
    adapted = adapt_tool_result(envelope, tool_name="workspace_navigator")
    assert adapted.status is ExecutionStatus.FAILED
    assert adapted.error is not None
    assert adapted.error.code == "WORKSPACE_DEVICE_OFFLINE"
    assert adapted.retryable is True


def test_unknown_content_type_raises_unsupported_contract_version():
    """无法无损转换时必须显式报错，不静默丢字段。"""
    with pytest.raises(UnsupportedContractVersion) as exc:
        adapt_tool_result({"status": "success", "data": {}, "content_type": "protobuf-v9"})
    assert exc.value.code == ContractErrorCode.UNSUPPORTED_CONTRACT_VERSION


def test_existing_execution_result_passes_through():
    original = ok({"a": 1}, tool_name="t")
    assert adapt_tool_result(original) is original


# ── 3. 四类投影都能产出且互不干扰 ──────────────────────────

def _navigator_payload() -> dict:
    return {
        "status": "partial",
        "action": "read",
        "summary": "已读取 答辩.pptx（第 1 页，共 40 页）",
        "data": {
            "path": "答辩.pptx",
            "sections": [
                {"source": "答辩.pptx", "location": "slide-1", "title": "第1页", "text": "第一页正文"},
                {"source": "答辩.pptx", "location": "slide-2", "title": "第2页", "text": "第二页正文"},
            ],
        },
        "has_more": True,
        "cursor": "cursor-next",
        "meta": {"workspace_id": "ws1", "workspace_version": 3, "char_count": 10},
    }


def test_all_four_projections_are_produced():
    result = ok(
        _navigator_payload(),
        tool_name="workspace_navigator",
        schema_name="lumi.workspace_navigator.result",
        call_id="c1",
    )
    views = default_projection_registry().project_all(result)
    assert set(views) == {"model", "ui", "audit", "storage"}
    # 模型投影：文本 + 预算 + 分页提示（正文可见）
    assert "第一页正文" in views["model"]["text"]
    assert views["model"]["has_more"] is True
    assert views["model"]["cursor"] == "cursor-next"
    # UI 投影：条目 + 分页 + 来源
    assert views["ui"]["has_more"] is True
    assert [item["location"] for item in views["ui"]["sections"]] == ["slide-1", "slide-2"]
    # 审计投影：不含正文，只有元数据与错误码
    assert views["audit"]["error_code"] is None
    assert "第一页正文" not in str(views["audit"])
    # 持久化投影：可恢复的最小快照（游标 + 版本）
    assert views["storage"]["cursor"] == "cursor-next"
    assert views["storage"]["workspace_version"] == 3


def test_model_projection_never_leaks_absolute_or_secret_fields():
    payload = {
        "status": "ok",
        "path": "src/main.py",
        "absolute_path": "/Users/someone/private/src/main.py",
        "cwd": "C:\\Users\\someone\\project",
        "secret_token": "sk-live-abcdef",
        "summary": "已读取 src/main.py",
        "sections": [{"source": "src/main.py", "location": "line-1", "text": "print('hi')"}],
    }
    view = default_projection_registry().project(ProjectionKind.MODEL, ok(payload, tool_name="navigator"))
    text = view["text"] + str(view)
    assert "print('hi')" in view["text"]
    assert "/Users/someone" not in text
    assert "C:\\\\Users" not in text
    assert "sk-live-abcdef" not in text


def test_model_projection_respects_budget_without_breaking_content():
    big = {
        "status": "ok",
        "sections": [{"source": "a.txt", "location": "line-1", "text": "x" * 20000}],
    }
    from lumi_contracts.projections import ModelProjection

    view = ModelProjection(budget=500)._project(ok(big, tool_name="navigator"))
    assert len(view["text"]) <= 500
    assert view["truncated"] is True


def test_projection_tolerates_unknown_payload_type():
    """未知 payload 不能让投影抛异常丢掉结果。"""
    view = default_projection_registry().project(ProjectionKind.MODEL, ok(FakePayload(), tool_name="mystery"))
    assert view["kind"] == "model"
    assert "degraded" not in view


def test_unregistered_kind_returns_safe_placeholder():
    from lumi_contracts import ProjectionRegistry

    registry = ProjectionRegistry()
    view = registry.project(ProjectionKind.UI, ok({"a": 1}))
    assert view["unregistered"] is True
    assert registry.project_all(ok({"a": 1})) == {}


# ── 4. 失败结果也能投影，且错误码稳定 ─────────────────────

def test_failure_result_projection_carries_stable_error_code():
    result = failure(
        ContractErrorCode.INVALID_INPUT,
        "path 必须是单个文件",
        tool_name="workspace_navigator",
        suggested_action="先 list 再 read",
    )
    views = default_projection_registry().project_all(result)
    assert views["model"]["error"]["code"] == "INVALID_INPUT"
    assert views["ui"]["error"]["suggested_action"] == "先 list 再 read"
    assert views["audit"]["error_code"] == "INVALID_INPUT"


# ── 5. 版本 / 事件 / 状态机 / 审批指纹 ─────────────────────

def test_contract_version_parse_and_compat():
    current = ContractVersion.parse("lumi.tool_response@2")
    assert current.name == "lumi.tool_response" and current.version == 2
    assert current.is_compatible_with("lumi.tool_response@1") is True
    assert current.is_compatible_with("lumi.tool_response@3") is False
    assert current.is_compatible_with("lumi.other@1") is False
    with pytest.raises(ValueError):
        ContractVersion.parse("tool_response")


def test_stream_event_frames_carry_version_and_sequence():
    seq = EventSequencer()
    first = seq.wrap(StreamEventType.DELTA, content="你好")
    second = seq.wrap(StreamEventType.DONE, status="completed")
    assert first.seq == 1 and second.seq == 2
    frame = first.to_sse()
    assert frame["type"] == "delta" and frame["version"] == 1 and frame["content"] == "你好"


def test_unknown_event_type_is_flagged_but_not_fatal():
    event = StreamEvent(type="future_event_from_newer_backend", data={"x": 1})
    assert event.is_known is False
    assert event.to_sse()["type"] == "future_event_from_newer_backend"


def test_run_state_transitions_are_guarded():
    assert can_transition(RunState.PENDING, RunState.RUNNING) is True
    assert can_transition(RunState.RUNNING, RunState.COMPLETED) is True
    assert can_transition(RunState.COMPLETED, RunState.RUNNING) is False
    assert can_transition(RunState.FAILED, RunState.RUNNING) is False


def test_approval_fingerprint_binds_exact_arguments():
    a = approval_fingerprint("workspace_write", {"path": "a.txt", "content": "1"}, "ctx")
    b = approval_fingerprint("workspace_write", {"content": "1", "path": "a.txt"}, "ctx")
    c = approval_fingerprint("workspace_write", {"path": "a.txt", "content": "2"}, "ctx")
    assert a == b, "参数顺序不同不应改变指纹"
    assert a != c, "参数内容不同必须改变指纹"


def test_server_context_is_frozen_and_derivable():
    from pydantic import ValidationError

    context = ServerContext(user_id="u1", workspace_id="ws1", device_id="dev1")
    with pytest.raises(ValidationError):
        context.user_id = "u2"  # type: ignore[misc]
    child = context.child(job_id="j1")
    assert child.user_id == "u1" and child.job_id == "j1"
