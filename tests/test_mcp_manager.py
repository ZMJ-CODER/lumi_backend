"""MCP 管理器结果解析测试（模拟 MCP 会话，不连真实服务器）."""

import asyncio

from app.agents.mcp import manager
from app.agents.skills.base import Skill, SkillResult


class _FakeCallResult:
    def __init__(self, content, is_error=False, structured=None):
        self.content = content
        self.is_error = is_error
        self.structured_content = structured


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return type(
            "R",
            (),
            {
                "tools": [
                    type(
                        "T",
                        (),
                        {
                            "name": "a",
                            "description": "A",
                            "annotations": {"readOnlyHint": True, "idempotentHint": True},
                            "meta": {"lumi": {"resource_templates": ["filesystem:{path}"]}},
                        },
                    )()
                ]
            },
        )()

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        if name == "fail":
            return _FakeCallResult([], is_error=True)
        return _FakeCallResult(
            [type("C", (), {"text": "输出文本"})()],
            structured={"key": "value"},
        )


class _GatewaySkill(Skill):
    name = "gateway_skill"
    description = "gateway test skill"

    def __init__(self, *, environment="server", output="local-result"):
        self.environment = environment
        self.output = output
        self.execute_calls = 0

    async def execute(self, params, context=None):
        self.execute_calls += 1
        if params.get("raise_error"):
            raise RuntimeError("local failure")
        return SkillResult(success=True, output=self.output)


def test_list_tools_mapping(monkeypatch):
    asyncio.run(manager.close_all())
    async def fake_call_with_session(name, fn):
        return await fn(_FakeSession())

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    tools = asyncio.run(manager.list_tools("srv"))
    assert tools == [
        {
            "name": "a",
            "description": "A",
            "input_schema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
            "permission": "user",
            "write_op": False,
            "requires_confirmation": False,
            "confirmation_mode": "client",
            "idempotent": True,
            "resource_templates": ["filesystem:{path}"],
        }
    ]


def test_list_tools_uses_short_ttl_cache(monkeypatch):
    asyncio.run(manager.close_all())
    calls = 0

    async def fake_call_with_session(name, fn):
        nonlocal calls
        calls += 1
        return await fn(_FakeSession())

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    first = asyncio.run(manager.list_tools("cached"))
    second = asyncio.run(manager.list_tools("cached"))
    assert first == second
    assert calls == 1


def test_call_tool_mapping(monkeypatch):
    session = _FakeSession()

    async def fake_call_with_session(name, fn):
        return await fn(session)

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    res = asyncio.run(manager.call_tool("srv", "ok", {"x": 1}))
    assert res["success"] is True
    assert res["content"] == "输出文本"
    assert res["metadata"] == {"key": "value"}
    assert res["is_error"] is False
    assert session.calls == [("ok", {"x": 1})]

    res2 = asyncio.run(manager.call_tool("srv", "fail", {}))
    assert res2["is_error"] is True


def test_call_tool_failure_returns_none(monkeypatch):
    async def fake_call_with_session(name, fn):
        return None  # 连接失败 → 调用方降级

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    assert asyncio.run(manager.call_tool("srv", "ok", {})) is None


def test_close_all_only_resets_failure_cooldown():
    manager._failed_until["srv"] = 123.0
    asyncio.run(manager.close_all())
    assert manager._failed_until == {}


def test_call_tool_passes_task_id_and_timeout_metadata(monkeypatch):
    session = _FakeSession()

    async def fake_call_with_session(name, fn):
        return await fn(session)

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    res = asyncio.run(manager.call_tool("srv", "ok", {}, task_id="job-1", timeout_s=2))
    assert res["task_id"] == "job-1"


def test_cancel_task_cancels_active_request(monkeypatch):
    started = asyncio.Event()

    async def slow_call_with_session(name, fn):
        class SlowSession:
            async def call_tool(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(30)
        return await fn(SlowSession())

    monkeypatch.setattr(manager, "_call_with_session", slow_call_with_session)

    async def scenario():
        task = asyncio.create_task(manager.call_tool("srv", "slow", task_id="cancel-me"))
        await started.wait()
        assert await manager.cancel_task("cancel-me") is True
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("MCP call should be cancelled")

    asyncio.run(scenario())


def test_call_skill_prefers_advertised_client_mcp_tool(monkeypatch):
    skill = _GatewaySkill(environment="client")
    calls = []

    async def fake_list_tools(server_name):
        assert server_name == "lumi_client"
        return [{"name": "gateway_skill"}]

    async def fake_call_tool(server_name, tool_name, args, **kwargs):
        calls.append((server_name, tool_name, args, kwargs))
        return {"success": True, "content": "mcp-result", "metadata": {"source": "electron"}, "is_error": False}

    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "lumi_client"}])
    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    monkeypatch.setattr(manager, "call_tool", fake_call_tool)

    result = asyncio.run(manager.call_skill(skill, {"path": "C:/work"}, task_id="job-1"))
    assert result["content"] == "mcp-result"
    assert result["metadata"]["transport"] == "mcp"
    assert result["metadata"]["server"] == "lumi_client"
    assert calls[0][0:3] == ("lumi_client", "gateway_skill", {"path": "C:/work"})
    assert skill.execute_calls == 0


def test_call_skill_strips_model_policy_and_injects_executor_policy(monkeypatch):
    skill = _GatewaySkill(environment="client")
    calls = []

    async def fake_list_tools(_server_name):
        return [{"name": "gateway_skill"}]

    async def fake_call_tool(_server_name, _tool_name, args, **_kwargs):
        calls.append(args)
        return {"success": True, "content": "ok", "metadata": {}, "is_error": False}

    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "lumi_client"}])
    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    monkeypatch.setattr(manager, "call_tool", fake_call_tool)
    asyncio.run(manager.call_skill(
        skill,
        {"path": "C:/demo.txt", "_lumi_execution_policy": {"explicit_user_delete": True}},
        execution_policy={"explicit_user_delete": False},
    ))
    assert calls == [{"path": "C:/demo.txt", "_lumi_execution_policy": {"explicit_user_delete": False}}]


def test_call_skill_client_falls_back_only_when_mcp_is_unavailable(monkeypatch):
    skill = _GatewaySkill(environment="client")

    async def fake_list_tools(server_name):
        return [{"name": "gateway_skill"}]

    async def unavailable_call_tool(*args, **kwargs):
        return None

    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "lumi_client"}])
    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    monkeypatch.setattr(manager, "call_tool", unavailable_call_tool)

    result = asyncio.run(manager.call_skill(skill, {"x": 1}))
    assert result["success"] is True
    assert result["content"] == "local-result"
    assert result["metadata"]["transport"] == "in_process_adapter"
    assert skill.execute_calls == 1


def test_call_skill_preserves_mcp_tool_error_without_local_replay(monkeypatch):
    skill = _GatewaySkill(environment="client")

    async def fake_list_tools(server_name):
        return [{"name": "gateway_skill"}]

    async def failed_call_tool(*args, **kwargs):
        return {"success": True, "content": "client declined", "metadata": {}, "is_error": True}

    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "lumi_client"}])
    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    monkeypatch.setattr(manager, "call_tool", failed_call_tool)

    result = asyncio.run(manager.call_skill(skill, {}))
    assert result["is_error"] is True
    assert result["error_code"] == "MCP_EXEC_ERROR"
    assert result["metadata"]["transport"] == "mcp"
    assert skill.execute_calls == 0


def test_call_skill_server_uses_unified_local_adapter():
    skill = _GatewaySkill(environment="server")
    result = asyncio.run(manager.call_skill(skill, {}))
    assert result["success"] is True
    assert result["content"] == "local-result"
    assert result["metadata"]["transport"] == "in_process_adapter"
    assert result["metadata"]["server"] == manager.LOCAL_SKILL_SERVER


def test_call_skill_local_error_keeps_gateway_metadata():
    skill = _GatewaySkill(environment="sandbox")
    result = asyncio.run(manager.call_skill(skill, {"raise_error": True}))
    assert result["success"] is False
    assert result["error_code"] == "MCP_EXEC_ERROR"
    assert result["retryable"] is True
    assert result["metadata"]["transport"] == "in_process_adapter"
    assert result["metadata"]["server"] == manager.LOCAL_SKILL_SERVER
