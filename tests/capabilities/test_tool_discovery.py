"""工具四层发现与 L0 准入契约测试。"""

import asyncio

from app.agents.skills.capability import ToolCapability
from app.agents.skills.discovery import (
    ToolDiscoverySession,
    apply_skill_allowlist,
    build_domain_groups,
    search_tools,
    validate_tool_entry,
)
from app.agents.skills.executor import execute_tool_call


def _cap(name: str, domain: str, tags: list[str], **kwargs) -> ToolCapability:
    return ToolCapability(
        name=name,
        domain=domain,
        description=f"{name} 工具",
        intent_tags=tags,
        use_when=["用户明确要求该能力"],
        do_not_use_when=["不属于该能力范围"],
        parameters={"type": "object", "properties": {}},
        **kwargs,
    )


def test_l0_entry_requires_governed_metadata():
    item = ToolCapability(name="x", description="x", parameters={"type": "object"})
    errors = validate_tool_entry(item)
    assert "缺少 domain" in errors
    assert "缺少 use_when" in errors
    assert "缺少 do_not_use_when" in errors


def test_l1_groups_and_l2_search_reduce_namespace():
    items = [
        _cap("order_read", "order", ["订单", "查询"]),
        _cap("shipment_track", "logistics", ["物流", "轨迹"]),
        _cap("web_search", "research", ["联网", "网页"]),
    ]
    groups = build_domain_groups(items)
    assert set(groups) == {"order", "logistics", "research"}
    found = search_tools("查询物流轨迹", items, limit=2)
    assert found[0].name == "shipment_track"
    assert "web_search" not in {item.name for item in found}


def test_l3_skill_allowlist_is_hard_filter():
    items = [_cap("order_read", "order", ["订单"]), _cap("web_search", "research", ["联网"])]
    assert [item.name for item in apply_skill_allowlist(items, ["web_search"])] == ["web_search"]
    assert [item.name for item in apply_skill_allowlist(items, None)] == ["order_read", "web_search"]


def test_l3_execution_rechecks_allowlist_before_lookup():
    result = asyncio.run(execute_tool_call(
        {"function": {"name": "web_search", "arguments": "{}"}},
        "u1",
        allowed_tools={"calculator"},
    ))
    assert result.success is False
    assert result.error_code == "SKILL_TOOL_FORBIDDEN"


def test_l1_domain_description_comes_from_policy_file():
    groups = build_domain_groups([_cap("web_search", "research", ["联网"])])
    assert "公共资料" in groups["research"].description


def test_discovery_cache_ignores_stale_policy(monkeypatch):
    class FakeRedis:
        async def get(self, _key):
            return '{"policy_version":"old","tools":[]}'

    monkeypatch.setattr("app.core.redis.get_redis", lambda: FakeRedis())
    session = ToolDiscoverySession()
    asyncio.run(session.load("u1", "c1"))
    assert session.loaded_tools == {}


def test_l2_scales_to_more_than_one_hundred_tools():
    items = [
        _cap(f"tool_{index:03d}", "research" if index % 2 else "document", ["查询", f"主题{index}"])
        for index in range(120)
    ]
    found = search_tools("查询 主题117", items, limit=5)
    assert len(found) <= 5
    assert found[0].name == "tool_117"


def test_deprecated_tool_is_not_discovered():
    items = [
        _cap("old_search", "research", ["查询"], status="deprecated", deprecated_by="new_search"),
        _cap("new_search", "research", ["查询"]),
    ]
    assert "old_search" not in {item.name for item in search_tools("查询", items)}


def test_strict_plugin_registration_rejects_missing_l0_metadata(monkeypatch):
    from app.agents.skills.base import Tool, SkillResult
    from app.agents.skills.registry import ToolRegistry

    class IncompleteSkill(Tool):
        name = "incomplete_test_skill"

        async def execute(self, params, context=None):
            return SkillResult(success=True, output="ok")

    monkeypatch.setattr("app.core.config.settings.SKILL_REGISTRY_STRICT", True)
    ToolRegistry.unregister(IncompleteSkill.name)
    try:
        import pytest

        with pytest.raises(ValueError, match="未通过 L0 准入"):
            ToolRegistry.register(IncompleteSkill(), source="plugin")
    finally:
        ToolRegistry.unregister(IncompleteSkill.name)


def test_l2_session_keeps_loaded_schema():
    session = ToolDiscoverySession()
    item = _cap("calculator", "system", ["计算"])
    session.add([item])
    session.add([item])
    # 主键从裸 name 改成**复合标识**（plugin|provider|name@version）：后者才能区分
    # "两个插件提供同名工具"，前者会让后加入的静默覆盖前一个。这里断言的是"重复 add
    # 只留一条"这一原有语义。
    assert len(session.loaded_tools) == 1
    assert next(iter(session.loaded_tools.values())).name == "calculator"
    from app.agents.skills.mandatory_tools import tool_identity

    assert next(iter(session.loaded_tools)) == tool_identity(item)
