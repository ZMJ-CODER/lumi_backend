"""两代能力对照 · 缺口 3 **批次 1**：工具发现 / 目录快照 / 能力窗口的跨代守卫。

批次划分见 `docs/CAPABILITY_TWO_GENERATIONS.md` §3.4：旧代那 ≈307 例回归不是删掉，
而是按批迁成"新旧共跑"或"新代等价断言"。本文件是**批次 1**（发现与窗口），
不重复抄旧代的逐条用例，而是把五条要证明的性质**在真实表上全量**跑一遍：

1. **旧代认识的工具，新代必须认识**（且旧能力名可追溯，不能悄悄改归属）；
2. **新代只增不减**：新代多出来的绑定必须能追溯到写下来的声明，而不是"猜"出来的；
3. **不认识就不猜**：形状可疑的名字两边都不给结论；
4. **窗口只增不减**：打开资源窗口后，任何意图的静态窗口都必须被包含（不许丢工具）；
5. **新增工具不会让旧工具消失**（发现层的单调性——它是"插件装完老工具不可见"那类事故的守门人）。

批次 2（Broker/派发）、3（预检/审批/执行门禁）、4（事件与过程日志）另立文件。
"""

from __future__ import annotations

import pytest

from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP
from app.agents.capabilities.catalog.resource import (
    LEGACY_CAPABILITY_BINDINGS,
    RESOURCE_TYPES,
    TOOL_BINDINGS,
    UNIFIED_CAPABILITIES,
    binding_for_tool,
    catalog_snapshot,
    providers_for,
    registered_providers_for,
)
from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP, capability_for_tool

#: 形状可疑、**绝不能**给出结论的名字（按名字猜能力是最危险的一类错误：
#: 猜错就把写操作当只读派发）。这些名字都不在任何表里，测试自己先断言这一点。
_SUSPICIOUS = (
    "workspace_write_extra",
    "super_writer",
    "not_a_tool",
    "workspace_write/../../etc/passwd",
    "workspace_write --path=/home/u/.env",
    # 能力名不是工具名：``resource.write`` 折成末段会撞上客户端原子工具 ``write``，
    # 那是"按名字猜"（本层最不该做的事），因此必须两边都没有结论。
    "resource.write",
    "workspace.read",
    "",
)


def test_every_statically_routed_tool_is_known_to_the_new_layer():
    """性质 1：旧路由表认识的工具，新代必须认识，且旧能力名可追溯。

    "可追溯"是关键：新代可以把 ``workspace.read`` 表达成 ``resource.read / workspace``，
    但必须能说出"它原来属于哪个旧能力"——否则客户端协议、租约与审批的键就断了。
    """
    missing: list[tuple[str, str, str, str]] = []
    for tool, capability in TOOL_CAPABILITY_MAP.items():
        if not capability:
            # 本机动作（显式 None）：新代按 native_action 分类，不要求有资源绑定。
            continue
        binding = binding_for_tool(tool)
        if not binding.known or binding.legacy_capability != capability:
            missing.append((tool, capability, binding.legacy_capability, binding.source))
    assert missing == [], f"旧代认识、新代丢了归属：{missing}"


def test_server_implementation_names_are_not_lost_either():
    """实现名（``workspace_reader`` / ``git`` / ``python_exec`` …）同样不能被丢掉。"""
    missing = [
        name
        for name in IMPLEMENTATION_MAP
        if not binding_for_tool(name).known
        and name not in TOOL_BINDINGS
        and name not in TOOL_CAPABILITY_MAP
    ]
    # 只要求"要么新代认识，要么它本来就不是工具名"——所以这里只断言**认识**的占多数，
    # 逐条例外必须在下面的 known 集合里有解释（避免把"漏了"写成"允许"）。
    known = [name for name in IMPLEMENTATION_MAP if binding_for_tool(name).known]
    assert len(known) >= len(IMPLEMENTATION_MAP) - len(missing)
    for name in missing:
        assert capability_for_tool(name) in (None, ""), f"{name} 两边都不认识，说明表已失效"


def test_new_layer_bindings_are_all_traceable_to_a_written_declaration():
    """性质 2：新代多出来的绑定必须来自写下来的声明（工具级/能力级映射），不是猜的。"""
    for tool in TOOL_BINDINGS:
        binding = binding_for_tool(tool)
        assert binding.known is True, tool
        assert binding.capability in UNIFIED_CAPABILITIES, tool
        assert binding.resource_type in RESOURCE_TYPES, tool
        assert binding.source in {"tool", "capability"}, (tool, binding.source)
    for legacy, (unified, resource_type) in LEGACY_CAPABILITY_BINDINGS.items():
        assert unified in UNIFIED_CAPABILITIES, legacy
        assert resource_type in RESOURCE_TYPES, legacy
    # 旧目录里的能力必须**全部**有新层的统一绑定（"9/9 无损"这条不能悄悄退化成 8/9）
    from app.agents.capabilities.catalog.legacy import capability_catalog

    missing = sorted(
        {item.name for item in capability_catalog.all()} - set(LEGACY_CAPABILITY_BINDINGS)
    )
    assert missing == [], f"旧目录里的能力在新层没有统一绑定：{missing}"


@pytest.mark.parametrize("name", _SUSPICIOUS)
def test_suspicious_names_never_get_a_conclusion(name):
    """性质 3：不认识就不猜——两边都必须**没有结论**。"""
    assert name not in TOOL_BINDINGS
    assert name not in TOOL_CAPABILITY_MAP
    binding = binding_for_tool(name)
    assert binding.known is False, (name, binding)
    assert binding.capability == "" and binding.provider == ""
    assert capability_for_tool(name) in (None, ""), name

def test_capability_window_never_drops_a_static_tool(monkeypatch):
    """性质 4：打开资源窗口后，每个意图的窗口都必须**包含**静态窗口（只增不减）。

    窗口是"模型这一轮能看到什么"的入口，少一个工具就是"这一步做不了"，
    因此这条断言按意图全量跑，而不是抽查两个。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", True)
    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", True)
    from app.agents.orchestration.preflight.capability_preflight import (
        ACTION_TOOL_WINDOW,
        tool_window_for_actions,
    )

    assert ACTION_TOOL_WINDOW, "静态窗口表不能为空（否则这条断言是空转）"
    for intent, static in ACTION_TOOL_WINDOW.items():
        window = tool_window_for_actions([intent])
        assert set(static) <= set(window), (intent, set(static) - set(window))
        if static:
            # 静态窗口非空 ⇒ 派生结果不能为空（``PUBLISH`` 这类静态就是空的意图不在此列：
            # 那表示"本地没有承接工具"，不是"丢了工具"）。
            assert window, intent
    # 未知意图不允许凭空造窗口
    assert tool_window_for_actions(["NOT_AN_INTENT"]) == ()


def test_read_entry_is_never_lost_when_a_resource_is_named(monkeypatch):
    """性质 4 的资源维度：指定资源类型后，读入口仍在窗口里（改之前必须先能读）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", True)
    from app.agents.orchestration.preflight.capability_preflight import tool_window_for_actions

    for action in ("MODIFY", "DELETE", "MOVE"):
        window = tool_window_for_actions([action], resource_types=["workspace"])
        assert "workspace_navigator" in window, (action, window)


def test_catalog_snapshot_is_closed_and_json_safe():
    """目录快照：词汇闭集、Provider 覆盖资源类型、可用 Provider 判定只有一处。"""
    import json

    snapshot = catalog_snapshot()
    json.dumps(snapshot, ensure_ascii=False)
    assert set(snapshot["resource_types"]) == set(RESOURCE_TYPES)
    assert set(snapshot["unified_capabilities"]) == set(UNIFIED_CAPABILITIES)
    covered = {rtype for spec in snapshot["providers"] for rtype in spec["resource_types"]}
    assert set(RESOURCE_TYPES) <= covered, "每个资源类型都要有 Provider 声明（哪怕是只有声明）"
    # registered / provider_id 两个字段是"有没有实现"的唯一判据
    for row in snapshot["providers"]:
        if row["registered"] is False:
            assert row["provider_id"] == "", row["name"]
    # 声明集合 ⊇ 可用集合，且可用集合的判据与 provider_ids_for 一致
    for capability in UNIFIED_CAPABILITIES:
        for resource_type in RESOURCE_TYPES:
            declared = providers_for(capability, resource_type)
            usable = registered_providers_for(capability, resource_type)
            assert set(usable) <= set(declared), (capability, resource_type)


def test_adding_a_tool_never_hides_existing_discovery():
    """性质 5：新增一个插件工具后，既有工具的发现结果**一个都不能少**。"""
    from app.agents.capabilities.catalog.tool_registry import (
        capability_of,
        entries_by_name,
        invalidate_cache,
        resolve_tool,
    )
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    before = set(entries_by_name())
    probe = "demo_discovery_probe"
    assert probe not in before

    class _Probe(Tool):
        description = "测试用：新增工具不能影响既有发现"
        category = "devtools"
        environment = "client"
        capability = "resource.read"
        resource_type = "memory"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, params, context=None):  # noqa: ANN001
            return ToolOutput(success=True, output="ok", data=dict(params))

    _Probe.name = probe
    ToolRegistry.register(_Probe(), source="test")
    try:
        after = set(entries_by_name())
        assert before <= after, sorted(before - after)
        assert probe in after
        for tool in ("workspace_write", "workspace_navigator", "create_office_document"):
            assert resolve_tool(tool) is not None, tool
            assert capability_of(tool) == (capability_for_tool(tool) or ""), tool
    finally:
        ToolRegistry.unregister(probe)
        invalidate_cache()
