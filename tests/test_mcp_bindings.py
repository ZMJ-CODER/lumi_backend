"""MCP 用户绑定治理：配置白名单、风险收紧和候选池隔离。"""

import asyncio
from types import SimpleNamespace

from app.agents.skills.capability import ToolCapability
from app.services import mcp_bindings


def test_only_configured_opt_in_servers_are_bindable(monkeypatch):
    monkeypatch.setattr(mcp_bindings.settings, "MCP_SERVERS", [
        {"name": "blocked"}, {"name": "allowed", "allow_user_binding": True},
    ])
    assert mcp_bindings.configured_external_server_names() == {"allowed"}
    assert asyncio.run(mcp_bindings.discover_bindable_tools("blocked")) == []


def test_external_capability_name_is_namespaced_and_metadata_is_explicit(monkeypatch):
    binding = SimpleNamespace(
        id="b1", server_name="approved", raw_tool_name="read_contract", description="读取合同",
        input_schema={"type": "object", "properties": {}}, domain="document",
        intent_tags=["合同", "读取"], scenes=["office"], permission="user", write_op=False,
        requires_confirmation=False, confirmation_mode="server", idempotent=True,
        resource_templates=[], status="enabled",
    )

    class _Result:
        def scalars(self):
            return self

        def __iter__(self):
            return iter([binding])

        def all(self):
            return [binding]

    class _Session:
        async def execute(self, _stmt):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Factory:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(mcp_bindings, "async_session_factory", _Factory())
    monkeypatch.setattr(mcp_bindings, "_configured_external_server", lambda _name: {"name": "approved"})
    monkeypatch.setattr("app.agents.mcp.manager.server_is_healthy", lambda _name: True)
    caps = asyncio.run(mcp_bindings.get_bound_capabilities("00000000-0000-0000-0000-000000000001", "office", "user"))
    assert len(caps) == 1
    assert caps[0].name == "mcp__approved__read_contract"
    assert caps[0].source == "mcp"
    # 已存在的早期绑定也不能绕过外部 MCP 的服务端参数指纹确认。
    assert caps[0].requires_confirmation is True
    assert caps[0].idempotent is True


def test_external_call_quota_rejects_when_redis_is_unavailable(monkeypatch):
    def unavailable_redis():
        raise RuntimeError("redis offline")

    monkeypatch.setattr(mcp_bindings, "get_redis", unavailable_redis)
    allowed, reason = asyncio.run(mcp_bindings.acquire_call_quota("binding", "user", 1, 1))
    assert allowed is False
    assert reason == "QUOTA_UNAVAILABLE"


def test_pending_binding_user_cannot_bypass_review(monkeypatch):
    binding = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000001", status="pending_approval",
    )

    class _Session:
        async def get(self, _model, _id):
            return binding

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Factory:
        def __call__(self):
            return _Session()

    monkeypatch.setattr(mcp_bindings, "async_session_factory", _Factory())
    assert asyncio.run(mcp_bindings.set_binding_enabled(binding.user_id, binding.id, True)) is False
