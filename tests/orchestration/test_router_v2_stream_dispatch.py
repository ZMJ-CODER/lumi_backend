"""Router v2 流式入口分发回归（场景 1-4）。

通过替换重依赖（prep/标题/office 上下文/路由评估/各类流），直接断言
``handle_message_stream`` 在 TASK_ROUTER_V2_ENABLED=True 下的分发行为：
  - direct_chat：不触发任何读取；
  - m1_atomic_read：走受控读取 → 真实 chat_stream 快路径；
  - m1_atomic_action / sequential_workflow / dynamic_agent：进入 Job 编排；
  - blocked：直接输出原因并只发一个 done。
"""

from __future__ import annotations

import asyncio
import types

from lumi_orch.execution_router import ExecutionMode, RouteDecision
from lumi_orch.safety_policy import SafetyAction
from lumi_orch.task_assessment import TaskProfile

from app.core.config import settings
from app.services.orchestrator import Orchestrator
from app.services.task_router_adapter import RoutedTask


def _routed(mode: ExecutionMode | None, *, blocked: bool = False, reason: str = "",
            safety: SafetyAction = SafetyAction.ALLOW) -> RoutedTask:
    return RoutedTask(
        profile=TaskProfile(complexity="M1"),
        decision=RouteDecision(mode, blocked=blocked, reason_code="BLOCKED" if blocked else "", reason=reason),
        safety_action=safety,
        assessor_source="test",
    )


def _install_stubs(monkeypatch, orch: Orchestrator, routed: RoutedTask, calls: dict):
    """替换 I/O 依赖，记录各条流是否被选中。"""

    async def fake_transcript(content, attachments):
        return content

    async def fake_prep(*args, **kwargs):
        return {"messages": [{"role": "user", "content": "x"}], "citations": [], "is_first": False}

    async def fake_title(*args, **kwargs):
        return ""

    async def fake_images(*args, **kwargs):
        return []

    async def fake_summary(*args, **kwargs):
        return ""

    async def fake_load_office_context(*args, **kwargs):
        from app.office.context import OfficeContext

        return OfficeContext()

    async def fake_shape(*args, **kwargs):
        return types.SimpleNamespace(requires_orchestration=False, reasons=())

    async def fake_plan_and_route(**kwargs):
        return routed

    def fake_preflight(*args, **kwargs):
        return types.SimpleNamespace(needs_clarification=False, reason="", question="")

    async def fake_finalize(*args, **kwargs):
        return None

    def make_stream(name):
        def factory(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1

            async def gen():
                yield {"type": "delta", "content": f"[{name}]"}

            return gen()

        return factory

    monkeypatch.setattr(orch, "_resolve_transcript", fake_transcript)
    monkeypatch.setattr(orch, "_prepare_chat", fake_prep)
    monkeypatch.setattr(orch, "get_conversation_title", fake_title)
    monkeypatch.setattr(orch, "_load_image_data_uris", fake_images)
    monkeypatch.setattr(orch, "_finalize_reply", fake_finalize)
    monkeypatch.setattr(orch, "_generate_title", fake_title)
    monkeypatch.setattr(orch, "_stream_llm_auto", make_stream("direct"))
    monkeypatch.setattr(orch, "_stream_v2_atomic_read", make_stream("atomic_read"))
    monkeypatch.setattr(orch, "_stream_office_job", make_stream("job"))

    async def fake_bounded_read(*args, **kwargs):
        calls["legacy_read"] = calls.get("legacy_read", 0) + 1
        return None, []

    monkeypatch.setattr(orch, "_bounded_workspace_read", fake_bounded_read)
    monkeypatch.setattr("app.services.orchestrator._office_workspace_summary_text", fake_summary)
    monkeypatch.setattr("app.office.context.load_office_context", fake_load_office_context)
    monkeypatch.setattr(
        "app.agents.orchestration.planning.task_shape.assess_task_shape_with_skills", fake_shape
    )
    monkeypatch.setattr(
        "app.agents.orchestration.preflight.task_preflight.preflight_external_effect", fake_preflight
    )
    monkeypatch.setattr("app.services.task_router_adapter.plan_and_route", fake_plan_and_route)


def _run(orch: Orchestrator, monkeypatch, *, routed, request, workspace_id=""):
    calls: dict = {}
    _install_stubs(monkeypatch, orch, routed, calls)
    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", True)

    async def scenario():
        return [
            event
            async for event in orch.handle_message_stream(
                user_id="u1",
                conversation_id="c1",
                content=request,
                scene="office",
                workspace_id=workspace_id or None,
            )
        ]

    return asyncio.run(scenario()), calls


def test_scenario_1_direct_chat_does_not_read(monkeypatch):
    orch = Orchestrator()
    events, calls = _run(
        orch, monkeypatch,
        routed=_routed(ExecutionMode.DIRECT_CHAT),
        request="把这句话改得更正式一些。",
    )
    assert calls.get("direct") == 1
    assert "atomic_read" not in calls and "job" not in calls and "legacy_read" not in calls
    meta_events = [e for e in events if e["type"] == "task_router"]
    assert meta_events and meta_events[0]["route_mode"] == "direct_chat"
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and done[0].get("task_router", {}).get("route_mode") == "direct_chat"


def test_scenario_2_workspace_read_uses_atomic_read(monkeypatch):
    orch = Orchestrator()
    events, calls = _run(
        orch, monkeypatch,
        routed=_routed(ExecutionMode.M1_ATOMIC_READ),
        request="看一下项目里的 README 说了什么",
        workspace_id="w1",
    )
    assert calls.get("atomic_read") == 1
    assert "direct" not in calls and "job" not in calls
    assert [e for e in events if e["type"] == "done"]


def test_scenario_3_and_4_atomic_action_enters_orchestration(monkeypatch):
    for request in ("跑一下这个 Python 脚本", "把工作区里的 config.json 端口改成 8080"):
        orch = Orchestrator()
        events, calls = _run(
            orch, monkeypatch,
            routed=_routed(ExecutionMode.M1_ATOMIC_ACTION),
            request=request,
            workspace_id="w1",
        )
        assert calls.get("job") == 1, request
        assert "direct" not in calls and "atomic_read" not in calls
        assert [e for e in events if e["type"] == "done"]


def test_complex_modes_enter_orchestration_even_when_shape_is_direct(monkeypatch):
    for mode in (ExecutionMode.SEQUENTIAL_WORKFLOW, ExecutionMode.DYNAMIC_AGENT):
        orch = Orchestrator()
        events, calls = _run(
            orch, monkeypatch,
            routed=_routed(mode),
            request="读文档再查资料然后生成报告",
            workspace_id="w1",
        )
        assert calls.get("job") == 1
        assert [e for e in events if e["type"] == "done"]


def test_blocked_router_returns_reason_and_single_done(monkeypatch):
    orch = Orchestrator()
    events, calls = _run(
        orch, monkeypatch,
        routed=_routed(None, blocked=True, reason="请先绑定工作区或本地设备"),
        request="读取工作区里的配置",
    )
    assert calls == {}  # 不触发任何流
    delta = "".join(e.get("content") or "" for e in events if e["type"] == "delta")
    assert "绑定工作区" in delta
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and done[0]["content"] == delta
