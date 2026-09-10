"""MCP 管理器结果解析测试（模拟 MCP 会话，不连真实服务器）."""

import asyncio

from app.agents.mcp import manager
from app.agents.mcp.desktop_connections import desktop_connections
from app.agents.skills.base import Tool, SkillResult


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


class _GatewaySkill(Tool):
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


def test_desktop_connection_registry_resolves_configured_loopback(monkeypatch):
    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "desktop", "url": "http://127.0.0.1:8765/mcp"}])
    endpoint = desktop_connections.resolve("desktop", user_id="user-1", device_id="device-1")
    assert endpoint is not None
    assert endpoint.url.endswith("/mcp")
    assert endpoint.device_id == "device-1"


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
    assert res["status"] == "success"
    assert res["data"] == {"key": "value"}
    assert res["content_type"] == "structured"
    assert res["error"] is None
    assert session.calls[0][0] == "ok"
    assert session.calls[0][1]["x"] == 1
    assert session.calls[0][1]["_lumi_call_id"]

    res2 = asyncio.run(manager.call_tool("srv", "fail", {}))
    assert res2["status"] == "failed"


def test_call_tool_failure_returns_none(monkeypatch):
    async def fake_call_with_session(name, fn):
        return None  # 连接失败 → 调用方降级

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    assert asyncio.run(manager.call_tool("srv", "ok", {})) is None


def test_close_all_only_resets_failure_cooldown():
    manager._failed_until["srv"] = 123.0
    asyncio.run(manager.close_all())
    assert manager._failed_until == {}


def test_loopback_health_url_is_restricted_to_local_http():
    assert manager._loopback_health_url({"url": "http://127.0.0.1:8765/mcp"}) == "http://127.0.0.1:8765/health"
    assert manager._loopback_health_url({"url": "http://localhost:8765/mcp"}) == "http://localhost:8765/health"
    assert manager._loopback_health_url({"url": "https://mcp.example.com/mcp"}) == ""


def test_local_mcp_restart_bypasses_failure_cooldown(monkeypatch):
    asyncio.run(manager.close_all())
    manager._failed_until["desktop"] = manager.time.monotonic() + 30
    calls = []

    monkeypatch.setattr(
        manager,
        "_server_cfg",
        lambda _name: {"name": "desktop", "url": "http://127.0.0.1:8765/mcp"},
    )

    async def recovered(_cfg):
        return True

    class FakeWorker:
        def __init__(self, _cfg):
            self.task = type("Task", (), {"done": lambda self: False})()

        async def call(self, fn):
            calls.append("called")
            return await fn(object())

        async def close(self):
            calls.append("closed")

    monkeypatch.setattr(manager, "_loopback_server_has_recovered", recovered)
    monkeypatch.setattr(manager, "_McpSessionWorker", FakeWorker)

    result = asyncio.run(manager._call_with_session("desktop", lambda _session: _async_value("ok")))
    assert result == "ok"
    assert calls == ["called"]
    assert "desktop" not in manager._failed_until
    asyncio.run(manager.close_all())


def test_ensure_server_healthy_clears_local_cooldown_and_cache(monkeypatch):
    asyncio.run(manager.close_all())
    manager._failed_until["desktop"] = manager.time.monotonic() + 30
    manager._tools_cache["desktop"] = (manager.time.monotonic(), [{"name": "old"}])
    monkeypatch.setattr(
        manager,
        "_server_cfg",
        lambda _name: {"name": "desktop", "url": "http://127.0.0.1:8765/mcp"},
    )

    async def recovered(_cfg):
        return True

    class FakeBreaker:
        async def record_success(self):
            return None

    monkeypatch.setattr(manager, "_loopback_server_has_recovered", recovered)
    monkeypatch.setattr(manager, "get_breaker", lambda _name: FakeBreaker())
    assert asyncio.run(manager.ensure_server_healthy("desktop")) is True
    assert "desktop" not in manager._failed_until
    assert "desktop" not in manager._tools_cache


async def _async_value(value):
    return value


def test_call_tool_passes_task_id_and_timeout_metadata(monkeypatch):
    session = _FakeSession()

    async def fake_call_with_session(name, fn):
        return await fn(session)

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    res = asyncio.run(manager.call_tool("srv", "ok", {}, task_id="job-1", timeout_s=2))
    assert res["meta"]["quality_hints"]["task_id"] == "job-1"


def test_call_tool_preserves_pending_approval_and_call_id(monkeypatch):
    class PendingSession(_FakeSession):
        async def call_tool(self, name, args, **kwargs):
            self.calls.append((name, args))
            return _FakeCallResult(
                [type("C", (), {"text": "等待审批"})()],
                structured={"call_id": "call-9", "status": "pending_approval", "data": {"base_version": 1}, "content_type": "structured", "meta": {"workspace_id": "ws-1"}},
            )
    session = PendingSession()
    async def fake_call_with_session(name, fn): return await fn(session)
    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    result = asyncio.run(manager.call_tool("srv", "workspace_commit", {}, call_id="call-9"))
    assert result["status"] == "pending_approval"
    assert result["call_id"] == "call-9"
    assert result["data"] == {"base_version": 1}
    assert result["meta"]["workspace_id"] == "ws-1"
    assert session.calls[0][1]["_lumi_call_id"] == "call-9"


def test_call_tool_prefers_structured_desktop_content_for_model_projection(monkeypatch):
    class ReadSession(_FakeSession):
        async def call_tool(self, name, args, **kwargs):
            return _FakeCallResult(
                [type("C", (), {"text": "hello workspace"})()],
                structured={"status": "success", "data": {"path": "a.txt", "content": "hello workspace"}, "content_type": "structured", "meta": {"workspace_id": "ws-1"}},
            )
    async def fake_call_with_session(_name, fn): return await fn(ReadSession())
    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    result = asyncio.run(manager.call_tool("srv", "workspace_read", {}))
    assert result["data"]["content"] == "hello workspace"
    assert result["meta"]["summary"] == "hello workspace"


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
    assert result["status"] == "success"
    assert result["data"] == "mcp-result"
    assert result["meta"]["quality_hints"]["transport"]["kind"] == "mcp"
    assert result["meta"]["quality_hints"]["transport"]["server"] == "lumi_client"
    assert calls[0][0:3] == ("lumi_client", "gateway_skill", {"path": "C:/work"})
    assert skill.execute_calls == 0


def test_call_skill_maps_legacy_project_sandbox_tool_to_workspace_mcp(monkeypatch):
    skill = _GatewaySkill(environment="client")
    skill.name = "run_in_sandbox"
    calls = []

    async def fake_list_tools(_server_name):
        return [{"name": "sandbox_run"}]

    async def fake_call_tool(_server_name, tool_name, args, **_kwargs):
        calls.append((tool_name, args))
        return {"status": "success", "data": "ok", "content_type": "text", "meta": {"workspace_id": "p-1"}}

    monkeypatch.setattr(manager.settings, "MCP_SERVERS", [{"name": "lumi_client"}])
    monkeypatch.setattr(manager, "list_tools", fake_list_tools)
    monkeypatch.setattr(manager, "call_tool", fake_call_tool)
    result = asyncio.run(manager.call_skill(skill, {"project_id": "p-1", "command": "pytest -q"}))
    assert result["status"] == "success"
    assert calls == [("sandbox_run", {"workspace_id": "p-1", "command": "pytest -q", "cwd": "", "timeout": 60})]


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
    assert result["status"] == "success"
    assert result["data"] == "local-result"
    assert result["meta"]["quality_hints"]["transport"]["kind"] == "in_process_adapter"
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
    assert result["status"] == "failed"
    assert result["error_code"] == "MCP_EXEC_ERROR"
    assert result["meta"]["quality_hints"]["transport"]["kind"] == "mcp"
    assert skill.execute_calls == 0


def test_call_skill_server_uses_unified_local_adapter():
    skill = _GatewaySkill(environment="server")
    result = asyncio.run(manager.call_skill(skill, {}))
    assert result["status"] == "success"
    assert result["data"] == "local-result"
    assert result["meta"]["quality_hints"]["transport"]["kind"] == "in_process_adapter"
    assert result["meta"]["quality_hints"]["transport"]["server"] == manager.LOCAL_SKILL_SERVER


def test_call_skill_local_error_keeps_gateway_metadata():
    skill = _GatewaySkill(environment="sandbox")
    result = asyncio.run(manager.call_skill(skill, {"raise_error": True}))
    assert result["status"] == "failed"
    assert result["error_code"] == "MCP_EXEC_ERROR"
    assert result["retryable"] is True
    assert result["meta"]["quality_hints"]["transport"]["kind"] == "in_process_adapter"
    assert result["meta"]["quality_hints"]["transport"]["server"] == manager.LOCAL_SKILL_SERVER
