"""MCP 管理器结果解析测试（模拟 MCP 会话，不连真实服务器）."""

import asyncio

from app.agents.mcp import manager


class _FakeCallResult:
    def __init__(self, content, is_error=False, structured=None):
        self.content = content
        self.is_error = is_error
        self.structured_content = structured


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return type("R", (), {"tools": [type("T", (), {"name": "a", "description": "A"})()]})()

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "fail":
            return _FakeCallResult([], is_error=True)
        return _FakeCallResult(
            [type("C", (), {"text": "输出文本"})()],
            structured={"key": "value"},
        )


def test_list_tools_mapping(monkeypatch):
    async def fake_call_with_session(name, fn):
        return await fn(_FakeSession())

    monkeypatch.setattr(manager, "_call_with_session", fake_call_with_session)
    tools = asyncio.run(manager.list_tools("srv"))
    assert tools == [{"name": "a", "description": "A"}]


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
