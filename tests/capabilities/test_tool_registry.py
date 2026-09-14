"""统一工具注册表（方案《工具发现链路》P1）回归。

三条主张，各有对应断言：

1. **默认关闭时行为逐字不变**——静态表仍是唯一真相源（这是能安全上线的底线）；
2. **派生结果与静态表当前零差异**——三个维度（工具→能力 / 能力→MCP 目标 /
   动作意图→工具窗口）都必须为空，否则不能切真相源；
3. **开关打开后能力等价**——同一批查询在两条路径下给出同一答案（含兼容写法）。
"""

from __future__ import annotations

import pytest

from app.agents.capabilities.views.tool_shadow import (
    shadow_compare,
)
from app.agents.capabilities.catalog.tool_registry import (
    ENV_CLIENT,
    INTENT_SIDE_EFFECTS,
    action_window,
    build_registry_entries,
    capability_of,
    entries_by_name,
    mcp_target_for,
    registry_derived_enabled,
    resolve_tool,
)


@pytest.fixture()
def derived(monkeypatch):
    """打开注册表派生路径（默认是关的）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", True)
    return settings


# ── 1. 默认关闭：静态表是唯一真相源 ────────────────────────


def test_flag_defaults_to_off():
    from app.core.config import settings

    assert settings.TOOL_REGISTRY_DERIVED is False
    assert registry_derived_enabled() is False


def test_flag_off_returns_static_results_verbatim(monkeypatch):
    """关闭时必须**逐字**等于静态表结果（不能悄悄换成派生）。"""
    from app.core.config import settings
    from app.agents.capabilities.registry.builtin import capability_for_tool, TOOL_CAPABILITY_MAP
    from app.agents.capabilities.broker.dispatch import mcp_tool_for_capability, CAPABILITY_TOOL_MAP

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", False)
    for tool in TOOL_CAPABILITY_MAP:
        assert capability_of(tool) == (capability_for_tool(tool) or ""), tool
    for capability in CAPABILITY_TOOL_MAP:
        assert mcp_target_for(capability) == mcp_tool_for_capability(capability), capability


def test_flag_off_action_window_falls_back_to_static_table(monkeypatch):
    from app.core.config import settings
    from app.agents.orchestration.preflight.capability_preflight import ACTION_TOOL_WINDOW

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", False)
    for intent, tools in ACTION_TOOL_WINDOW.items():
        assert action_window(intent, fallback=tools) == tools, intent


# ── 2. 派生与静态零差异（切换真相源的前提）─────────────────


def test_shadow_compare_reports_no_divergence_today():
    """四个**判定维度**必须无差异。有差异时先修派生逻辑，**不要**切开关。

    其余维度必须是**已登记披露**的那些（声明档位 / 声明窗口补位 / 资源层窗口）：
    它们预览"打开开关后会变成什么"，有意为之。这条断言的价值在于——**没有第三种可能**：
    任何既不是判定、又没被登记为披露的差异都会在这里失败（前端也是按这个口径拒绝
    "未登记维度"的）。
    """
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_DECLARED_DIMENSION,
        SHADOW_DECLARED_DIMENSIONS,
        SHADOW_PARITY_DIMENSIONS,
    )

    diffs = shadow_compare()
    assert set(SHADOW_PARITY_DIMENSIONS) <= set(diffs)
    for dimension in SHADOW_PARITY_DIMENSIONS:
        assert diffs.get(dimension) == [], f"{dimension} 派生与静态表存在差异：{diffs.get(dimension)}"
    assert set(diffs) - set(SHADOW_PARITY_DIMENSIONS) <= set(SHADOW_DECLARED_DIMENSIONS), (
        "出现了未登记的差异维度"
    )
    assert SHADOW_DECLARED_DIMENSION not in diffs, "当前不该有任何工具声明档位"


def test_shadow_diff_rows_are_uniformly_three_cells():
    """**形状契约**：所有影子维度的行都是三格 ``[名字, 静态值, 派生值]``。

    前端按统一形状渲染（`normalizeShadowDiff` 只读前三格，列名按维度语义配）。
    曾经 ``tool→risk_tier(declared)`` 多塞了一格"生效档位"，结果前端把生效档位
    当成"声明档位"显示——两边都没报错，但语义已经错了。这条断言把它钉住。
    """
    diffs = shadow_compare()
    sizes = {dimension: {len(row) for row in rows} for dimension, rows in diffs.items()}
    for dimension, row_sizes in sizes.items():
        assert row_sizes <= {3}, f"{dimension} 的行形状不是三格：{row_sizes}"


def test_shadow_parity_totals_treat_missing_dimension_as_unsafe():
    """维度没算成 ≠ 一致：缺失维度必须让 switch_safe 变 False。

    （曾经的实现用 ``all(not v for v in diffs.values())``——某一维度因为异常没进字典时，
    "没跑成"会被读成"没有差异"，这是最危险的一种误报。）
    """
    from app.agents.capabilities.views.tool_shadow import shadow_parity_totals

    partial = {"tool→capability": [], "capability→mcp_target": []}
    totals = shadow_parity_totals(partial)
    assert totals["switch_safe"] is False
    assert "intent→tool_window" in totals["missing_dimensions"]
    assert "tool→risk_tier" in totals["missing_dimensions"]


def test_derived_action_window_matches_static_exactly(derived):
    """打开开关后，动作窗口必须与静态表**逐条相同**。"""
    from app.agents.orchestration.preflight.capability_preflight import ACTION_TOOL_WINDOW

    for intent, tools in ACTION_TOOL_WINDOW.items():
        assert sorted(action_window(intent, fallback=tools)) == sorted(tools), intent


def test_derived_capability_lookup_matches_static(derived):
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    for tool, capability in TOOL_CAPABILITY_MAP.items():
        entry = resolve_tool(tool)
        assert entry is not None, tool
        assert entry.capability == (capability or ""), tool


def test_derived_mcp_target_matches_static(derived):
    from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP

    for capability, tool in CAPABILITY_TOOL_MAP.items():
        assert mcp_target_for(capability) == tool, capability


# ── 3. 查询入口的形状与兼容性 ──────────────────────────────


def test_resolve_tool_accepts_all_three_name_forms(derived):
    """裸名 / mcp__server__tool / server.tool 三种写法都要认得（生产里都出现过）。"""
    for name in ("workspace_navigator", "mcp__lumi_client__workspace_navigator", "lumi.workspace_navigator"):
        entry = resolve_tool(name)
        assert entry is not None, name
        assert entry.name == "workspace_navigator", name
        assert entry.capability == "workspace.read", name


def test_entry_carries_all_seven_derivations():
    """条目必须回答七个派生点（缺一个就意味着还要回去查静态表）。"""
    entry = entries_by_name()["workspace_write"]
    payload = entry.as_dict()
    for key in (
        "capability",        # 能力映射
        "provider_id",       # Provider 路由
        "mcp_target",        # MCP 目标
        "action_intents",    # 动作窗口
        "approval_policy",   # 审批策略
        "execution_env",     # 执行环境
        "scenes",            # 可见场景
    ):
        assert key in payload, key
    assert entry.capability == "workspace.write"
    assert entry.action_type == "write"
    assert entry.requires_lease is True, "local_only 能力必须由客户端租约承载"


def test_local_capability_routes_to_client_forwarder():
    """本地数据能力一律表达为"由客户端做"——这是安全边界，不是性能选择。"""
    from app.agents.capabilities.registry.builtin import PROVIDER_CLIENT_REMOTE, PROVIDER_SERVER_ARTIFACT

    read_entry = entries_by_name()["workspace_navigator"]
    assert read_entry.provider_id == PROVIDER_CLIENT_REMOTE
    assert read_entry.data_locality == "local_only"
    artifact = entries_by_name().get("create_office_document")
    assert artifact is not None
    assert artifact.provider_id == PROVIDER_SERVER_ARTIFACT, "artifact.create 是唯一服务端内联能力"
    assert artifact.execution_env in {"server", ENV_CLIENT}


def test_plugin_tool_without_catalog_entry_is_still_resolvable():
    """新插件的工具只要注册了，注册表就得认得它（不能因为目录里没有就返回 None）。"""
    entry = resolve_tool("workspace_catalog")
    assert entry is not None
    assert entry.capability == "workspace.read"
    # 本机动作：没有能力，也不参与租约。
    local_action = resolve_tool("desktop_open_url")
    assert local_action is not None
    assert local_action.capability == ""
    assert local_action.requires_lease is False


def test_unknown_tool_resolves_to_none():
    """未知工具必须返回 None（宁可报"未知"，也不要按关键词猜一个能力）。"""
    assert resolve_tool("totally_unknown_tool") is None


def test_registry_entries_cover_static_tables_and_specs():
    names = set(entries_by_name())
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    assert set(TOOL_CAPABILITY_MAP) <= names
    assert len(names) >= len(TOOL_CAPABILITY_MAP)


def test_action_window_of_unknown_intent_is_empty(derived):
    assert action_window("NOT_AN_INTENT", fallback=()) == ()


def test_intent_side_effect_table_is_the_bridge():
    """动作窗口派生靠这张桥接表：它错了，窗口就错了。"""
    assert INTENT_SIDE_EFFECTS["READ"] == frozenset({"read"})
    assert INTENT_SIDE_EFFECTS["DELETE"] == frozenset({"delete"})
    assert INTENT_SIDE_EFFECTS["EXECUTE"] == frozenset({"execute"})


def test_modify_intent_includes_the_read_entry(derived):
    """改文件之前必须先能读到内容——派生的 MODIFY 窗口必须带读取入口。"""
    window = action_window("MODIFY", fallback=())
    assert "workspace_navigator" in window
    assert "workspace_edit" in window


def test_build_registry_entries_from_runtime_capabilities():
    """给定运行期候选池时，条目从 ``ToolCapability`` 派生（审批/环境/场景用它）。

    注意 ``scenes``：``ToolCapability`` 没有顶层 ``scenes`` 字段，场景信息在
    ``annotations`` 里（Skill 注册时写入）。这条断言就是防止"以为有顶层字段"——
    派生必须同时看两处，否则场景过滤会永远拿到空集。
    """
    from app.agents.skills.capability import ToolCapability

    item = ToolCapability(
        name="workspace_write",
        description="写工作区",
        environment="client",
        write_op=True,
        requires_confirmation=True,
        annotations={"scenes": ["office"]},
    )
    rows = build_registry_entries([item])
    assert len(rows) == 1
    entry = rows[0]
    assert entry.capability == "workspace.write"
    assert entry.execution_env == ENV_CLIENT
    assert entry.action_type == "write"
    assert entry.approval_policy == "confirm"
    assert entry.requires_confirmation is True
    assert entry.scenes == ("office",)


def test_global_cache_reacts_to_tools_registered_after_first_build():
    """缓存键必须含**工具注册表版本**（不能只靠 ToolSpec 摘要）。

    实测过的失效路径：插件的两部分是分开落地的——ToolSpec 先登记、Skill 稍后才加载。
    中间那一刻若建过全局条目缓存，ToolSpec 摘要与能力目录指纹都不变，只有运行期声明
    变了；缓存键不含注册表版本时，该条目会被**永久**缓存，之后所有查询（发现/预检/
    派发）都读到"能力与资源类型为空"的旧值，直到进程重启。

    这里用 ``register → unregister → entries_by_name() → register`` 复现同一种不对称：
    ``unregister`` 只撤运行期 Skill、**不动**已登记的 ToolSpec，因此第二次注册的 spec
    摘要与第一次逐字相同——epoch 若只由 spec/静态表/目录决定，就会停在陈旧条目上。
    """
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    name = "demo_late_resource_writer"

    class _LateResourceWriter(Tool):
        """只靠两条类属性接入统一资源能力层的新工具。"""

        description = "测试用：声明式接入的写入工具"
        category = "devtools"
        environment = "client"
        capability = "resource.write"
        resource_type = "memory"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, params, context=None):  # noqa: ANN001
            return ToolOutput(success=True, output="ok", data=dict(params))

    _LateResourceWriter.name = name
    ToolRegistry.register(_LateResourceWriter(), source="test")
    ToolRegistry.unregister(name)
    try:
        # Skill 不在注册表里（ToolSpec 还在）：此刻条目只能是"空绑定"。
        stale = entries_by_name().get(name)
        assert stale is not None, "ToolSpec 已登记，条目必须存在"
        assert stale.unified_capability == "", "Skill 未加载时不可能派生出声明的资源能力"

        ToolRegistry.register(_LateResourceWriter(), source="test")
        entry = entries_by_name().get(name)
        assert entry is not None, "后注册的工具必须立刻出现在条目表里"
        assert entry.unified_capability == "resource.write"
        assert entry.resource_type == "memory"
        assert entry.resource_provider == "memory_provider"
        # 缺口 2a：memory 只有声明（registered=False）⇒ 候选为空，不能当成可用。
        assert entry.provider_candidates == ()
        assert "capability:declared" in entry.sources
    finally:
        ToolRegistry.unregister(name)

