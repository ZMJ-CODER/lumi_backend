"""v2 验收追踪（policy_v2_acceptance 日志行）回归测试。"""

from __future__ import annotations

from app.services.policy_acceptance import (
    finish_trace,
    log_sse_event,
    log_sse_start,
    new_trace,
    record_trace_event,
    sse_detail,
)


def _events():
    return [
        {"type": "task_policy", "execution_policy": "single_tool_then_stream"},
        {"type": "process", "content": "正在读取工作区资料"},
        {"type": "tool_started", "call_id": "c1", "tool": "workspace_read"},
        {"type": "tool_completed", "call_id": "c1", "status": "success"},
        {"type": "process", "content": "已完成资料整理"},
        {"type": "delta", "content": "README 主要介绍"},
        {"type": "delta", "content": "项目结构与构建方式。"},
        {"type": "done", "content": "README 主要介绍项目结构与构建方式。"},
    ]


def test_trace_records_sequence_counts_and_first_delta():
    trace = new_trace(enabled=True)
    assert trace is not None
    for event in _events():
        record_trace_event(trace, event)

    summary = finish_trace(
        trace,
        user_id="u1",
        conversation_id="c1",
        policy_public={
            "task_profile": {"complexity": "ATOMIC", "goal": "RETRIEVE"},
            "execution_policy": "single_tool_then_stream",
            "policy_version": "v2",
        },
    )
    assert summary is not None
    # 事件顺序完整（连续 delta 折叠为 delta×N，关键事件不被淹没）
    assert summary["event_sequence"] == [
        "task_policy", "process", "tool_started", "tool_completed",
        "process", "delta×2", "done",
    ]
    assert summary["done_count"] == 1
    assert summary["delta_count"] == 2
    assert summary["tool_event_count"] == 2
    assert summary["execution_policy"] == "single_tool_then_stream"
    assert summary["policy_version"] == "v2"
    # first_delta 只从首个 delta 计（process/tool 不影响）
    assert summary["first_delta_latency_ms"] is not None
    assert summary["route_latency_ms"] is not None
    assert summary["planner_invoked"] is False
    assert summary["agent_invoked"] is False
    assert summary["delta_chars"] == len("README 主要介绍项目结构与构建方式。")


def test_trace_disabled_is_noop():
    assert new_trace(enabled=False) is None


def test_route_latency_uses_decision_override():
    """route_latency_ms 取“画像+路由决策”耗时，而不是到首个 delta 的时间。"""
    trace = new_trace(enabled=True)
    trace["route_latency_ms_override"] = 7
    for event in [
        {"type": "task_router", "route_mode": "m1_atomic_read"},
        {"type": "delta", "content": "x" * 30},
        {"type": "done", "status": "waiting_next", "content": ""},
    ]:
        record_trace_event(trace, event)
    summary = finish_trace(trace)
    assert summary["route_latency_ms"] == 7
    assert summary["first_delta_latency_ms"] is not None
    assert summary["first_delta_latency_ms"] >= 0


def test_trace_double_done_detected():
    trace = new_trace(enabled=True)
    for event in _events() + [{"type": "done", "content": ""}]:
        record_trace_event(trace, event)
    summary = finish_trace(trace)
    assert summary["done_count"] == 2  # 双 done 会被验收日志直接暴露


def test_sse_detail_never_leaks_full_text():
    assert sse_detail({"type": "delta", "content": "x" * 500}) == "chars=500"
    assert "process" not in sse_detail({"type": "delta", "content": "x"})
    assert "route_mode=direct_chat" in sse_detail({
        "type": "task_router",
        "route_mode": "direct_chat",
        "safety_action": "ALLOW",
        "task_profile": {"complexity": "M0"},
    })
    assert sse_detail({"type": "done", "status": "waiting_next", "content": "abc"}) == (
        "status=waiting_next content_chars=3"
    )


def test_acceptance_log_helpers_are_callable(capsys):
    # 只是保证开关路径可调用且不抛异常（日志内容由 loguru 输出）
    log_sse_start(conversation_id="c1", content="把这句话改得更正式一些。", scene="office")
    log_sse_event(
        conversation_id="c1", sequence=1, elapsed_ms=42,
        event={"type": "task_router", "route_mode": "direct_chat",
               "safety_action": "ALLOW", "task_profile": {"complexity": "M0"}},
    )
