"""resume 前置校验与协议事件适配器的回归测试。"""

from __future__ import annotations

import asyncio

from lumi_execution.step_resume import (
    ResumeCheckInput,
    validate_resume_request,
)
from lumi_orch.protocol import protocol_events_from_stream


def test_resume_valid_request_allows():
    result = validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        current_step_id="step_1", expected_step_id="step_1",
        plan_revision=1, current_revision=1,
        idempotency_key="run-step-1", seen_keys=(),
    ))
    assert result.allowed is True


def test_resume_ownership_and_terminal_states():
    assert validate_resume_request(ResumeCheckInput(
        user_id="u2", job_owner="u1", job_state="waiting_run")).allowed is False
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="completed")).allowed is False
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="running")).allowed is False


def test_resume_step_and_revision_mismatch():
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        current_step_id="step_1", expected_step_id="step_2")).allowed is False
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        plan_revision=2, current_revision=1)).allowed is False


def test_resume_idempotency_and_bindings():
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        idempotency_key="k1", seen_keys=("k1",))).allowed is False
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        idempotency_key="k2", workspace_bound=False)).allowed is False
    assert validate_resume_request(ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_run",
        idempotency_key="k3", dependencies_done=False)).allowed is False


def test_protocol_stream_adapter_hides_dsml_and_emits_events():
    async def source():
        yield ("我先查看一下这份PPT的内容。\n"
               '<||DSML||invoke name="workspace_content_extract" arguments=\'{"path": "a.pptx"}\'/>')
        yield " 之后继续"

    async def collect():
        return [event async for event in protocol_events_from_stream(source())]

    events = asyncio.run(collect())
    types = [event["type"] for event in events]
    assert "process" in types
    assert "tool" in types
    assert "delta" in types
    process_text = "".join(e["content"] for e in events if e["type"] == "process")
    answer_text = "".join(e["content"] for e in events if e["type"] == "delta")
    assert "我先查看一下" in process_text
    assert "之后继续" in answer_text
    combined = process_text + answer_text
    assert "<||DSML||" not in combined
    tool_events = [e for e in events if e["type"] == "tool"]
    assert tool_events[0]["tool_call"]["name"] == "workspace_content_extract"


def test_protocol_stream_adapter_reports_warning_on_bad_protocol():
    async def source():
        yield "准备处理。\n<||DSML||invoke name=\"workspace_read\""
        yield "（未闭合）"

    async def collect():
        return [event async for event in protocol_events_from_stream(source())]

    events = asyncio.run(collect())
    assert any(event["type"] == "warning" for event in events)
    leaked = "".join(
        e.get("content", "") for e in events if e["type"] in {"delta", "process"}
    )
    assert "<||DSML||" not in leaked
