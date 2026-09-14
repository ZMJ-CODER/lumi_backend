"""Agent 任务气泡的后端契约回归：统一过程字段 + 安全摘要 + 去重 + 快照持久化。

对应"不要新建第二套消息入口"：过程字段在**既有 SSE 出口**（SseEventEncoder）
补齐，普通聊天的 delta/done 帧保持原样。
"""

from __future__ import annotations

import json

from lumi_contracts import (
    JobRunView,
    ProcessKind,
    ProcessLogEntry,
    ProcessStatus,
    RunState,
    derive_kind,
    merge_process_log,
    sanitize_process_text,
)

from app.contracts.events import SseEventEncoder


def _payload(line: str) -> dict:
    return json.loads(line[len("data: "):].strip())


# ── kind 由后端判定 ──────────────────────────────────────────────


def test_kind_is_derived_by_backend_not_guessed_by_frontend():
    assert derive_kind(tool_name="workspace_navigator") is ProcessKind.READ
    assert derive_kind(tool_name="Read") is ProcessKind.READ
    assert derive_kind(tool_name="workspace_stage_write") is ProcessKind.EDIT
    assert derive_kind(tool_name="apply_patch") is ProcessKind.EDIT
    assert derive_kind(tool_name="Bash") is ProcessKind.COMMAND
    assert derive_kind(tool_name="run_in_sandbox") is ProcessKind.COMMAND
    assert derive_kind(tool_name="unknown_thing") is ProcessKind.TOOL
    assert derive_kind(event_type="process") is ProcessKind.THINKING
    assert derive_kind(event_type="step_started") is ProcessKind.THINKING
    # 显式声明优先
    assert derive_kind(tool_name="Bash", explicit="read") is ProcessKind.READ


# ── 安全摘要：不落原始参数/响应/路径/凭据 ────────────────────────


def test_safe_summary_strips_paths_credentials_and_raw_payloads():
    assert "/Users/someone/secret" not in sanitize_process_text(
        "读取 /Users/someone/secret/a.py 完成", limit=200
    )
    assert "C:\\Users\\me\\proj" not in sanitize_process_text(
        "写入 C:\\Users\\me\\proj\\a.py", limit=200
    )
    assert "sk-live-abcdef123456" not in sanitize_process_text(
        "Authorization: Bearer sk-live-abcdef123456", limit=200
    )
    raw = '<dsml><tool_calls><invoke name="x"><parameter name="a">1</parameter></invoke>'
    assert "parameter" not in sanitize_process_text(raw, limit=200)


def test_entry_never_carries_raw_arguments_or_reasoning():
    entry = ProcessLogEntry.from_event(
        {
            "type": "tool_started",
            "tool_name": "workspace_navigator",
            "arguments": {"path": "/etc/passwd", "token": "sk-x"},
            "result": {"raw": "<dsml>secret</dsml>"},
            "reasoning": "模型内部完整推理过程",
            "summary": "正在读取工作区资料",
            "detail": "已读取第 1 个文件",
            "call_id": "call-1",
            "step_id": "step-1",
        },
        job_id="job-1",
        sequence=7,
    )
    dumped = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
    assert entry.kind is ProcessKind.READ
    assert entry.status is ProcessStatus.RUNNING
    assert entry.call_id == "call-1" and entry.step_id == "step-1"
    assert entry.sequence == 7
    for forbidden in ("/etc/passwd", "sk-x", "dsml", "推理过程", "arguments"):
        assert forbidden not in dumped


# ── SSE 出口：统一字段，普通帧不变 ───────────────────────────────


def test_process_and_tool_frames_carry_unified_fields():
    encoder = SseEventEncoder(job_id="job-1")
    tool = _payload(encoder.encode({
        "type": "tool_started",
        "tool_name": "workspace_stage_write",
        "summary": "正在生成修改方案",
        "call_id": "call-9",
        "step_id": "step-2",
    }))
    assert tool["kind"] == "edit"
    assert tool["status"] == "running"
    assert tool["entry_id"]
    assert tool["call_id"] == "call-9" and tool["step_id"] == "step-2"
    assert tool["sequence"] == 1
    assert tool["occurred_at"]
    assert tool["version"] == 1

    step = _payload(encoder.encode({"type": "step_completed", "summary": "步骤完成", "step_id": "step-2"}))
    assert step["kind"] == "thinking"
    assert step["status"] == "completed"
    assert step["sequence"] == 2
    assert step["job_id"] == "job-1"


def test_plain_chat_frames_are_untouched():
    encoder = SseEventEncoder(conversation_id="c1")
    delta = _payload(encoder.encode({"type": "delta", "content": "你好"}))
    assert delta["content"] == "你好"
    assert "kind" not in delta and "entry_id" not in delta
    done = _payload(encoder.encode({"type": "done", "content": "完整回答"}))
    assert done["content"] == "完整回答"
    assert "kind" not in done
    assert done["conversation_id"] == "c1"


# ── 去重与快照持久化 ─────────────────────────────────────────────


def test_merge_process_log_dedups_across_sse_poll_and_refresh():
    first = [{"entry_id": "process-12", "kind": "read", "summary": "正在读取", "status": "running"}]
    # SSE 重连/轮询/刷新后又收到同一条（状态推进为 completed）
    second = [{"entry_id": "process-12", "kind": "read", "summary": "正在读取", "status": "completed"}]
    merged = merge_process_log(first, second)
    assert len(merged) == 1
    assert merged[0].status is ProcessStatus.COMPLETED

    # 工具条目用稳定 call_id 去重
    tools = merge_process_log(
        [{"call_id": "call-1", "job_id": "job-1", "kind": "command", "summary": "运行测试"}],
        [{"call_id": "call-1", "job_id": "job-1", "kind": "command", "status": "failed"}],
    )
    assert len(tools) == 1 and tools[0].status is ProcessStatus.FAILED


def test_run_view_persists_process_log_outside_routing():
    view = JobRunView(job_id="job-1", conversation_id="c1", status=RunState.RUNNING)
    view = view.with_process_log([
        {"entry_id": "process-1", "kind": "thinking", "summary": "正在定位相关文件", "sequence": 1},
        {"entry_id": "process-2", "kind": "read", "summary": "正在读取工作区资料", "sequence": 2, "status": "completed"},
    ])
    snapshot = view.to_snapshot()
    assert [row["summary"] for row in snapshot["process_log"]] == [
        "正在定位相关文件",
        "正在读取工作区资料",
    ]
    # 过程日志不放 routing（routing 只存路由/策略）
    assert "process_log" not in snapshot["routing"]
    # 顺序稳定，可被前端按 sequence 渲染
    assert [row["sequence"] for row in snapshot["process_log"]] == [1, 2]


def test_process_log_is_bounded_rolling_window():
    rows = [
        {"entry_id": f"p-{index}", "kind": "read", "summary": f"第 {index} 条", "sequence": index}
        for index in range(260)
    ]
    merged = merge_process_log([], rows, limit=200)
    assert len(merged) == 200
    assert merged[-1].entry_id == "p-259"
