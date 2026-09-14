"""DAG 节点工具执行互斥测试。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.skills.base import Tool, SkillResult
from app.agents.skills.executor import execute_tool_call
from app.agents.skills.registry import SkillRegistry, ToolRegistry
from lumi_orch.resources import ResourceCoordinator


class _ProbeSkill(Tool):
    scenes = ["office"]
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, params, context=None) -> SkillResult:
        return SkillResult(success=True, output="unused")


def _call(name: str, scope: str = "job-lock-test") -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


@pytest.fixture
def _isolated_registry(monkeypatch):
    import app.agents.resource_coordination as resources
    import app.agents.skills.executor as executor

    SkillRegistry.clear()
    ToolRegistry.clear()
    monkeypatch.setattr(executor.settings, "AGENT_BASE_TOOLS_ONLY", False)
    # Unit tests deliberately use the kernel's process-local coordinator. The
    # production adapter replaces this with a Redis-backed lease.
    coordinator = ResourceCoordinator(fail_closed=lambda _claim: False)
    monkeypatch.setattr(resources, "resource_coordinator", coordinator)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(executor, "_record_skill_log", noop)
    monkeypatch.setattr(executor, "_record_skill_telemetry", noop)
    yield
    ToolRegistry.clear()


def test_same_tool_is_serialized_within_one_job(_isolated_registry, monkeypatch):
    import app.agents.mcp.manager as manager

    skill = _ProbeSkill()
    skill.name = "tool_lock_same"
    skill.description = "互斥探针"
    ToolRegistry.register(skill)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0
    calls = 0

    async def fake_call_skill(*_args, **_kwargs):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return {"success": True, "content": "ok", "metadata": {}, "is_error": False}

    monkeypatch.setattr(manager, "call_skill", fake_call_skill)

    async def scenario():
        first = asyncio.create_task(
            execute_tool_call(_call(skill.name), "user-1", "office", execution_scope="job-1")
        )
        await entered.wait()
        second = asyncio.create_task(
            execute_tool_call(_call(skill.name), "user-1", "office", execution_scope="job-1")
        )
        await asyncio.sleep(0.03)
        assert calls == 1
        assert max_active == 1
        release.set()
        results = await asyncio.gather(first, second)
        assert all(result.success for result in results)
        assert calls == 2
        assert max_active == 1

    asyncio.run(scenario())


def test_different_tools_can_execute_in_parallel(_isolated_registry, monkeypatch):
    import app.agents.mcp.manager as manager

    for name in ("tool_lock_first", "tool_lock_second"):
        skill = _ProbeSkill()
        skill.name = name
        skill.description = name
        ToolRegistry.register(skill)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_call_skill(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return {"success": True, "content": "ok", "metadata": {}, "is_error": False}

    monkeypatch.setattr(manager, "call_skill", fake_call_skill)

    async def scenario():
        first = asyncio.create_task(
            execute_tool_call(_call("tool_lock_first"), "user-1", "office", execution_scope="job-1")
        )
        second = asyncio.create_task(
            execute_tool_call(_call("tool_lock_second"), "user-1", "office", execution_scope="job-1")
        )
        await asyncio.wait_for(entered.wait(), timeout=0.3)
        assert max_active == 2
        release.set()
        results = await asyncio.gather(first, second)
        assert all(result.success for result in results)

    asyncio.run(scenario())


def test_exact_registered_tool_execution_does_not_load_remote_bindings(_isolated_registry, monkeypatch):
    import app.agents.skills.executor as executor
    import app.services.mcp_bindings as bindings

    skill = _ProbeSkill()
    skill.name = "tool_exact_fast_path"
    skill.description = "精确工具快速路径探针"
    ToolRegistry.register(skill)

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("exact local tool lookup must not query MCP bindings")

    monkeypatch.setattr(bindings, "get_bound_capabilities", must_not_run)
    capability = asyncio.run(executor.get_tool_capability(skill.name, "office", "user", "user-1"))
    assert capability is not None
    assert capability.name == skill.name
