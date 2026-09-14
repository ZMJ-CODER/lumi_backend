"""标准事件信封契约回归（方案第一阶段：内部一套事件 + 两种 SSE 投影）。

锁死四件事：

1. **信封字段只有一套权威定义**：``event_id/version/seq/type/trace_id/
   conversation_id/job_id/occurred_at/payload`` —— 没有 ``data``、没有
   ``job_run_id``；
2. **事件类型收敛表**：``delta → text_delta``、``step`` 双态、``done`` →
   ``control(completed)`` + 兼容 ``done``、``task_failed`` → ``control(failed)``
   + 兼容 ``error``；
3. **``thought_delta`` 不传原始思维链**：``reasoning`` / ``thought`` /
   ``arguments`` / ``messages`` / 凭据永不进入任何投影；
4. **旧投影逐字节不变**（前端当前消费的形状），新投影可开关切换。
"""

from __future__ import annotations

import json

from app.contracts.events import STREAM_EVENT_VERSION, SseEventEncoder
from app.contracts.ui_projection import (
    execution_result_events,
    terminal_control_event,
)
from lumi_contracts import (
    ArtifactRef,
    ExecutionResult,
    JobRunView,
    build_envelope,
    canonical_events_for,
    dedupe_envelopes,
    failure,
    ok,
)
from lumi_contracts.common.status import ExecutionStatus

CANONICAL_KEYS = {
    "event_id", "version", "schema_version", "seq", "type", "trace_id",
    "conversation_id", "job_id", "occurred_at", "payload",
}

#: 一份"什么都塞进来"的脏事件：原始思维链、参数、凭据、原始结果。
DIRTY_EVENT = {
    "type": "process",
    "content": "正在读取 D:\\Users\\me\\secret\\config.py",
    "entry_id": "process:1",
    "kind": "read",
    "reasoning_content": "SECRET-COT-不要外发",
    "chain_of_thought": "SECRET-COT-2",
    "thought_delta": "SECRET-COT-3",
    "arguments": {"path": "D:\\Users\\me\\secret\\config.py"},
    "messages": [{"role": "system", "content": "SECRET-PROMPT"}],
    "api_key": "sk-abcdef123456",
    "raw_result": "SECRET-RAW-RESULT",
}


def _frames(encoder: SseEventEncoder, event: dict) -> list[dict]:
    return [json.loads(line[len("data: "):].strip()) for line in encoder.encode_all(event)]


# ── 1. 信封字段 ──────────────────────────────────────────


def test_canonical_envelope_has_exactly_one_field_set():
    frame = build_envelope("delta", {"content": "hi"}, seq=1, job_id="job-1").to_canonical_frame()
    assert set(frame) == CANONICAL_KEYS
    assert "data" not in frame, "旧版传输字段不得出现在标准信封里"
    assert "job_run_id" not in frame, "沿用既有 job_id，不新增 job_run_id"
    assert frame["version"] == 1, "协议版本必须 ≤ 前端支持版本（否则整条流被降级丢弃）"
    assert frame["schema_version"] == 1, "载荷结构版本（Schema 注册表注册维度）"
    assert frame["payload"]["content"] == "hi"
    assert frame["occurred_at"], "occurred_at 必须是 ISO 时间字符串"


def test_both_projections_come_from_the_same_event():
    event = {"type": "delta", "content": "## 总体概况\n", "message_id": "m1"}
    legacy = SseEventEncoder(job_id="job-1").frame(event)
    canonical = SseEventEncoder(job_id="job-1", protocol="canonical").canonical_frames(event)[0]
    # 旧投影：扁平字段 + version/seq（前端形状不变）
    assert legacy["type"] == "delta" and legacy["version"] == STREAM_EVENT_VERSION
    assert legacy["content"] == "## 总体概况\n"
    # 新投影：同一份内容进 payload，类型收敛成 text_delta
    assert canonical["type"] == "text_delta"
    assert canonical["payload"]["content"] == "## 总体概况\n"
    assert canonical["payload"]["format"] == "markdown"


# ── 2. 类型收敛表 ────────────────────────────────────────


def test_legacy_type_mapping_table():
    cases = {
        "delta": ["text_delta"],
        "process": ["process"],
        "tool_started": ["step_started"],
        "step_started": ["step_started"],
        "tool_completed": ["step_completed"],
        "step_completed": ["step_completed"],
        "approval_required": ["approval_required"],
        "approval_resolved": ["approval_resolved"],
        "error": ["error"],
        # 终态：标准 control + 兼容伴随帧
        "done": ["control", "done"],
        "task_failed": ["control", "error"],
    }
    for legacy_type, expected in cases.items():
        events = canonical_events_for(legacy_type, {"status": "completed"}, job_id="j1")
        assert [item.type for item in events] == expected, legacy_type


def test_step_dual_state_type_follows_status():
    running = canonical_events_for("step", {"step_id": "s1", "status": "running"}, job_id="j1")
    done = canonical_events_for("step", {"step_id": "s1", "status": "completed"}, job_id="j1")
    failed = canonical_events_for("step", {"step_id": "s1", "status": "failed"}, job_id="j1")
    assert [item.type for item in running] == ["step_started"]
    assert [item.type for item in done] == ["step_completed"]
    assert [item.type for item in failed] == ["step_completed"]
    assert done[0].payload["status"] == "completed"


def test_terminal_control_state_is_explicit():
    assert canonical_events_for("done", {}, job_id="j1")[0].payload["state"] == "completed"
    assert canonical_events_for("task_failed", {}, job_id="j1")[0].payload["state"] == "failed"
    assert terminal_control_event(state="cancelled", job_id="j1").payload["state"] == "cancelled"


def test_done_frame_status_overrides_the_completed_default():
    """终态帧上的 status/job_status 优先：失败/取消不能因为类型是 done 就报 completed。"""
    from app.contracts.event_adapter import canonical_events

    for status in ("failed", "cancelled", "interrupted", "waiting_next"):
        events = canonical_events({"type": "done", "job_id": "j1", "status": status})
        assert events[0].type == "control"
        assert events[0].payload["state"] == status
    # 没有状态字段时才退回"完成"
    assert canonical_events({"type": "done", "job_id": "j1"})[0].payload["state"] == "completed"
    # job_status 与 status 等价（job 快照用的名字）
    assert canonical_events({"type": "done", "job_id": "j1", "job_status": "failed"})[0].payload["state"] == "failed"


def test_task_router_metadata_survives_the_payload_whitelist():
    """路由元数据事件（task_router）的审计字段必须完整保留（不白屏、不丢状态）。"""
    from app.contracts.event_adapter import canonical_events

    events = canonical_events({
        "type": "task_router",
        "job_id": "j1",
        "task_profile": {"complexity": "M1"},
        "route_mode": "m1_atomic_read",
        "route_reason_code": "ok",
        "safety_action": "allow",
        "assessor_source": "heuristic",
        "policy_version": "router_v2",
    })
    payload = events[0].payload
    assert events[0].type == "task_router"
    assert payload["task_profile"] == {"complexity": "M1"}
    assert payload["route_mode"] == "m1_atomic_read"
    assert payload["assessor_source"] == "heuristic"


# ── 3. 原始思维链/参数/凭据永不外发 ──────────────────────


def test_raw_chain_of_thought_and_arguments_never_reach_the_envelope():
    encoder = SseEventEncoder(job_id="job-1", protocol="canonical")
    blob = json.dumps(_frames(encoder, DIRTY_EVENT), ensure_ascii=False)
    for forbidden in (
        "SECRET-COT", "SECRET-PROMPT", "SECRET-RAW-RESULT", "sk-abcdef123456",
        "reasoning_content", "chain_of_thought", "thought_delta",
        "arguments", "raw_result",
    ):
        assert forbidden not in blob, f"原始内容泄漏到事件：{forbidden}"
    # 绝对路径与凭据被净化，过程摘要仍然可展示
    assert "D:\\Users" not in blob
    assert "正在读取" in blob


def test_view_payload_rejects_unknown_view_type():
    events = canonical_events_for("view_updated", {"view_id": "v1", "view_type": "iframe_js", "data": {"script": "x"}}, job_id="j1")
    assert events[0].type == "view_updated"
    assert events[0].payload["view_type"] == "", "非白名单视图类型必须被收敛为空"
    assert events[0].payload["data"] == {}, "类型被拒时数据不得跟着下发（纵深防御）"
    known = canonical_events_for("view_updated", {"view_id": "v2", "view_type": "table", "data": {"rows": 1}}, job_id="j1")
    assert known[0].payload["view_type"] == "table"
    assert known[0].payload["data"] == {"rows": 1}


def test_text_delta_is_not_truncated_so_markdown_survives():
    """正文增量不做展示级截断：标题/换行/代码围栏必须原样穿过事件层。"""
    body = "## 总体概况\n\n- 列表项\n\n```python\nif __name__ == '__main__':\n    print(1)\n```\n" * 40
    frame = build_envelope("delta", {"content": body}, seq=1, job_id="j1").to_canonical_frame()
    assert frame["payload"]["content"] == body


# ── 4. 去重与序号 ────────────────────────────────────────


def test_event_id_is_stable_for_the_same_logical_event():
    first = canonical_events_for("process", {"entry_id": "process:1", "summary": "读取"}, job_id="j1")[0]
    second = canonical_events_for("process", {"entry_id": "process:1", "summary": "读取"}, job_id="j1")[0]
    assert first.event_id and first.event_id == second.event_id
    assert len(dedupe_envelopes([first, second])) == 1


def test_canonical_frames_get_monotonic_seq_and_unique_ids():
    encoder = SseEventEncoder(job_id="job-1", protocol="canonical")
    frames = encoder.canonical_frames({"type": "delta", "content": "a"})
    frames += encoder.canonical_frames({"type": "done", "status": "completed"})
    seqs = [item["seq"] for item in frames]
    assert seqs == sorted(set(seqs)) and len(seqs) == 3, seqs
    ids = [item["event_id"] for item in frames]
    assert all(ids) and len(set(ids)) == len(ids)
    assert encoder.last_seq == max(seqs)


def test_process_entry_sequence_is_bound_to_the_stream_seq():
    """过程条目的 entry_id/sequence 与旧投影同规则，重连后可去重。"""
    encoder = SseEventEncoder(job_id="job-1", protocol="canonical")
    frame = encoder.canonical_frames({"type": "process", "content": "正在读取配置文件"})[0]
    assert frame["payload"]["entry_id"] == "seq:job-1:1"
    assert frame["payload"]["sequence"] == 1
    assert frame["payload"]["summary"] == "正在读取配置文件"


# ── 5. 未知类型与前向兼容 ────────────────────────────────


def test_unknown_event_type_is_forwarded_not_rejected():
    frames = SseEventEncoder(job_id="job-1", protocol="canonical").canonical_frames(
        {"type": "future_event_from_newer_backend", "capability": "x", "unknown_field": {"a": 1}}
    )
    assert frames[0]["type"] == "future_event_from_newer_backend"
    # 未登记键被白名单丢弃，但已登记的展示字段保留
    assert frames[0]["payload"].get("capability") == "x"
    assert "unknown_field" not in frames[0]["payload"]


def test_capability_and_operation_frames_keep_their_display_fields():
    for event_type, payload in (
        ("capability_started", {"capability": "workspace.read", "provider_id": "lumi_client", "status": "running"}),
        ("operation_completed", {"operation": "write", "logical_path": "a.py", "status": "success"}),
    ):
        frames = SseEventEncoder(job_id="job-1", protocol="canonical").canonical_frames(
            {"type": event_type, **payload}
        )
        assert frames[0]["type"] == event_type
        for key, value in payload.items():
            assert frames[0]["payload"].get(key) == value


# ── 6. ExecutionResult → UI 事件 ─────────────────────────


def test_execution_result_projects_to_step_artifact_and_refs():
    result = ok(
        {"summary": "写入 README.md"},
        tool_name="workspace_write",
        call_id="c9",
    ).model_copy(update={
        "status": ExecutionStatus.SUCCESS,
        "job_id": "job-1",
        "artifact_refs": [ArtifactRef(ref_id="a1", name="README.md", media_type="text/markdown", size=12)],
        "output": "RAW-OUTPUT-BODY",
    })
    events = execution_result_events(result, step_id="s1", job_id="job-1")
    types = [item.type for item in events]
    assert types == ["step_completed", "artifact_created"]
    completed = events[0].payload
    assert completed["step_id"] == "s1"
    assert completed["status"] == "completed"
    assert completed["result_ref"]["tool"] == "workspace_write"
    assert completed["result_ref"]["call_id"] == "c9"
    # 事件里只有摘要与引用，没有原始输出正文
    assert "RAW-OUTPUT-BODY" not in json.dumps(events[0].payload, ensure_ascii=False)
    assert events[1].payload["artifact_id"] == "a1"
    assert "expires_at" in events[1].payload


def test_execution_failure_projects_to_unified_error_event():
    """失败结果 → ``step_completed`` + ``error``；错误载荷是**统一错误模型**。

    方案 §3 的硬约束：只有 ``code`` / ``category`` / ``retryable`` / ``safe_message``
    / ``detail_ref`` 出门，原始异常文本（``message``/``error``）永不外发；
    未登记的码保留原码（排障），文案走净化后的安全文本。
    """
    result = failure("REVISION_REQUIRED", "覆盖已有文件必须带 expected_revision", tool_name="workspace_write")
    events = execution_result_events(result, step_id="s2", job_id="job-1")
    assert [item.type for item in events] == ["step_completed", "error"]
    assert events[0].payload["status"] == "failed"
    assert events[0].payload["error_code"] == "REVISION_REQUIRED"
    # 用户仍能看到"要带 expected_revision"这条可执行信息（走净化后的步骤摘要）
    assert "expected_revision" in events[0].payload["output_summary"]

    err = events[1].payload
    assert err["code"] == "REVISION_REQUIRED", "未登记码保留原码，便于排障"
    assert err["safe_message"], "前端展示的是 safe_message"
    assert err["category"] in {"transient", "fatal", "business", "needs_human"}
    assert isinstance(err["retryable"], bool)
    assert "message" not in err, "原始异常文本不得进入公开事件（方案 §3.3）"
    assert "detail_ref" in err, "完整细节只能通过受权限保护的引用获取"


def test_pending_approval_projects_to_approval_required():
    result = ExecutionResult(status=ExecutionStatus.PENDING_APPROVAL, tool_name="workspace_delete")
    events = execution_result_events(result, step_id="s3", job_id="job-1")
    assert [item.type for item in events] == ["step_completed", "approval_required"]
    approval = events[1].payload
    assert approval["step_id"] == "s3"
    assert approval["action"] == "workspace_delete"


# ── 7. 持久化快照 ────────────────────────────────────────


def test_job_run_view_keeps_seq_watermark_and_artifact_refs():
    view = JobRunView(job_id="job-1").note_seq(4).note_seq(2).note_seq(7)
    assert view.last_seq == 7, "水位只增不减"
    view = view.with_artifacts([{"artifact_id": "a1", "filename": "x.md"}])
    view = view.with_artifacts([{"artifact_id": "a1", "expires_at": "2026-01-01"}, {"artifact_id": "a2"}])
    assert [item["artifact_id"] for item in view.artifact_refs] == ["a1", "a2"]
    assert view.artifact_refs[0]["expires_at"] == "2026-01-01"
    snapshot = view.to_snapshot()
    assert "final_answer" in snapshot and "process_log" in snapshot and "views" in snapshot
