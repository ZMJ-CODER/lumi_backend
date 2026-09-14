"""审批档位派生（P1 工具链路）回归：**对拍 + 声明只能收紧**。

三条主张：

1. **派生与静态零差异**——注册表负责的每个既有工具，打开 ``TOOL_REGISTRY_DERIVED``
   后档位必须与纯静态词表逐条相同（切开关不改行为）；
2. **声明的落点**——插件工具的类属性 / Provider 的能力对象 / 能力描述符三个渠道
   都要能进来，且都能被审批引擎看见；
3. **声明只能收紧**——"自述不算授权"：Provider 把档位声明得比事实更松时，
   必须以可信基线为准（否则一个第三方声明就能把工作区写入变成静默自动执行）。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def derived(monkeypatch):
    """打开注册表派生路径（默认关闭，静态词表是唯一真相源）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", True)
    return settings


@pytest.fixture()
def demo_tool(monkeypatch):
    """注册一个可声明档位的插件式工具，退出时清理。"""
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    registered: list[str] = []

    def _make(name: str, **declared):
        attrs = {
            "name": name,
            "description": "测试工具",
            "category": "devtools",
            "environment": "server",
            "parameters_schema": {"type": "object", "properties": {}},
            **declared,
        }

        async def execute(self, params, context=None):  # noqa: ANN001
            return ToolOutput(status="success", output="ok", data=dict(params))

        cls = type(f"_{name.title().replace('_', '')}Tool", (Tool,), {**attrs, "execute": execute})
        ToolRegistry.register(cls(), source="plugin")
        registered.append(name)
        return cls

    yield _make
    for name in registered:
        ToolRegistry.unregister(name)


@pytest.fixture()
def catalog_with(monkeypatch):
    """把能力目录临时替换成一个只有给定描述符的目录（测"插件新能力"的声明路径）。

    ``_capability_meta()`` 每次都从 ``catalog`` 模块取 ``capability_catalog``，
    因此替换那个模块属性即可；同时使注册表派生缓存失效（缓存键含目录指纹）。
    """
    from app.agents.capabilities.catalog import legacy as catalog_module
    from app.agents.capabilities.catalog.legacy import CapabilityCatalog
    from app.agents.capabilities.catalog.tool_registry import invalidate_cache

    def _apply(*descriptors):
        monkeypatch.setattr(
            catalog_module, "capability_catalog", CapabilityCatalog(tuple(descriptors))
        )
        invalidate_cache()

    yield _apply
    invalidate_cache()


# ── 1. 对拍：派生 == 静态（切换真相源的前提）───────────────


def test_every_registry_tool_matches_static_tier(derived):
    """逐条对拍：注册表负责的工具，派生档位必须等于纯静态词表档位。

    基线用 ``static_tier_of``（纯静态），**不能**用 ``classify_tool_risk``——
    后者已经混入派生，对拍会永远显示一致，等于没对拍。
    """
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_PARITY_DIMENSIONS,
        shadow_compare,
        shadow_parity_totals,
    )
    from app.agents.capabilities.catalog.tool_registry import (
        entries_by_name,
        risk_tier_of,
    )
    from app.agents.skills.approval_policy import static_tier_of

    assert set(SHADOW_PARITY_DIMENSIONS) <= set(shadow_compare())
    totals = shadow_parity_totals()
    assert totals["switch_safe"] is True, shadow_compare()

    checked = 0
    for name in entries_by_name():
        tier = risk_tier_of(name, {})
        if tier is None:
            continue  # 遗留/辅助工具：注册表明确不派生，由静态词表负责
        assert tier == static_tier_of(name, {})[0], name
        checked += 1
    assert checked >= 20, f"对拍覆盖太少（{checked}），说明派生没覆盖到真实工具"


def test_legacy_tools_are_explicitly_not_derived():
    """遗留工具的 ``None`` 是**有意契约**，不是"派生不出来"的兜底。

    调用方看到 ``None`` 必须回落静态词表；把它当 ``auto`` 会直接放大审批面。
    """
    from app.agents.capabilities.catalog.tool_registry import LEGACY_TIER_TOOLS, risk_tier_of

    for name in sorted(LEGACY_TIER_TOOLS):
        assert risk_tier_of(name, {}) is None, name
    assert risk_tier_of("", {}) is None


def test_approval_engine_agrees_with_registry_when_flag_on(derived):
    """审批引擎（``classify_tool_risk``）必须与注册表给出同一档位。"""
    from app.agents.capabilities.catalog.tool_registry import entries_by_name, risk_tier_of
    from app.agents.skills.approval_policy import classify_tool_risk

    for name in entries_by_name():
        tier = risk_tier_of(name, {})
        if tier is None:
            continue
        assert classify_tool_risk(name, {})[0] == tier, name


# ── 2. 声明渠道：插件工具 / Provider 能力 / 能力描述符 ──────


def test_tool_class_declaration_is_honored(derived, demo_tool):
    """插件类属性 ``risk_tier`` 就是声明位：注册一次即可生效，不用改审批词表。"""
    from app.agents.capabilities.catalog.tool_registry import declared_tier_of, risk_tier_of

    demo_tool("plugin_dangerous_tool", risk_tier="critical")
    assert declared_tier_of("plugin_dangerous_tool") == "critical"
    assert risk_tier_of("plugin_dangerous_tool", {}) == "critical"


def test_invalid_declared_tier_is_ignored(demo_tool):
    """大小写/空白容错，但**认不出来的值**必须当"未声明"（退回派生），
    不能就近取一个更容易放行的值。"""
    from app.agents.capabilities.catalog.tool_registry import declared_tier_of

    demo_tool("plugin_typo_tool", risk_tier="Auto ")
    assert declared_tier_of("plugin_typo_tool") == "auto"
    demo_tool("plugin_typo_tool_2", risk_tier="whatever")
    assert declared_tier_of("plugin_typo_tool_2") == ""
    demo_tool("plugin_typo_tool_3", risk_tier="critical!")
    assert declared_tier_of("plugin_typo_tool_3") == ""


def test_tool_declaration_reaches_capability_and_contract_spec(demo_tool):
    """声明必须随能力对象与契约 ``ToolSpec`` 一起流转（否则别的调用点看不见）。"""
    from app.agents.skills.executor import _skill_capability
    from app.agents.skills.registry import ToolRegistry
    from app.contracts.tools import tool_spec_from

    demo_tool("plugin_declaring_tool", risk_tier="critical", approval_policy="confirm")
    tool = ToolRegistry.get("plugin_declaring_tool")
    capability = _skill_capability(tool)
    assert capability.risk_tier == "critical"
    assert capability.approval_policy == "confirm"
    spec = tool_spec_from(tool)
    assert spec.risk_level.value == "critical"
    assert spec.requires_approval is True, "声明 C 档却不声明需审批，契约自检会自相矛盾"


def test_tool_class_approval_policy_is_honored(demo_tool):
    """``approval_policy`` 是策略声明位：显式 ``none`` 也要被尊重。"""
    from app.agents.capabilities.catalog.tool_registry import approval_policy_of

    demo_tool("plugin_silent_tool", approval_policy="none")
    assert approval_policy_of("plugin_silent_tool") == "none"
    demo_tool("plugin_confirm_tool", approval_policy="confirm")
    assert approval_policy_of("plugin_confirm_tool") == "confirm"


def test_provider_capability_declaration_can_tighten(derived):
    """Provider 把档位声明在**运行期能力对象**上：传进来就必须被采纳（收紧）。"""
    from app.agents.capabilities.catalog.tool_registry import risk_tier_of
    from app.agents.skills.capability import ToolCapability

    item = ToolCapability(
        name="workspace_write",
        environment="client",
        write_op=True,
        risk_tier="critical",
    )
    assert risk_tier_of("workspace_write", {}, item) == "critical"


def test_capability_descriptor_declarations_map_to_tiers(catalog_with):
    """能力描述符的声明（副作用 + 本机确认）→ 档位；这是插件新能力的唯一入口。"""
    from lumi_contracts.plugins import CapabilityDescriptor, DataLocality, SideEffectKind

    from app.agents.capabilities.catalog.tool_registry import capability_declared_tier

    def _tier_for(*, effects, needs_local: bool) -> str:
        catalog_with(
            CapabilityDescriptor(
                name="demo.capability",
                side_effects=list(effects),
                data_locality=DataLocality.CLOUD,
                needs_local_confirmation=needs_local,
            )
        )
        return capability_declared_tier("demo.capability")

    assert _tier_for(effects=[SideEffectKind.READ], needs_local=False) == "auto"
    assert _tier_for(effects=[SideEffectKind.READ], needs_local=True) == "routine"
    assert _tier_for(effects=[SideEffectKind.WRITE], needs_local=True) == "routine"
    # external（对外发送/发布）收不回来，必须是 C 档——漏映射会掉进"默认 routine"。
    assert _tier_for(effects=[SideEffectKind.EXTERNAL], needs_local=False) == "critical"
    assert _tier_for(effects=[SideEffectKind.NETWORK], needs_local=False) == "routine"
    # 什么都不声明的能力：不猜，交给调用方的保守默认（未知 → C 档）。
    assert _tier_for(effects=[], needs_local=True) == ""


def test_descriptor_needs_local_confirmation_drives_approval_policy(catalog_with):
    """描述符的 ``needs_local_confirmation`` 必须转成"要确认"，而不是只活在文档里。

    同时钉住优先级：**工具级例外比能力级信号更具体**——``workspace_diff`` 属于
    ``git.operations``（该能力要求本机确认），但它自己只读、静态档位就是 A，
    不能被能力级要求盖成"要确认"。
    """
    from lumi_contracts.plugins import CapabilityDescriptor, DataLocality, SideEffectKind

    from app.agents.capabilities.catalog.tool_registry import (
        _derive_policy,
        _needs_local_confirmation,
        capability_declared_tier,
    )

    catalog_with(
        CapabilityDescriptor(
            name="workspace.read",
            side_effects=[SideEffectKind.READ],
            data_locality=DataLocality.LOCAL_ONLY,
            needs_local_confirmation=True,
        )
    )
    assert _needs_local_confirmation("workspace.read") is True
    # 只读能力 + 本机确认 → 抬到 B 档（A 档不会弹确认，与声明矛盾）。
    assert capability_declared_tier("workspace.read") == "routine"
    # 没有工具级例外的工具：描述符要求生效。
    assert _derive_policy("plugin_brand_new_tool", "workspace.read") == "confirm"
    # 有工具级例外的工具（A 档只读）：工具级事实优先。
    assert _derive_policy("workspace_read", "workspace.read") == "none"
    assert _derive_policy("workspace_navigator", "workspace.read") == "none"


# ── 3. 声明只能收紧（不能放宽）──────────────────────────


def test_declaration_cannot_loosen_capability_baseline(derived):
    """自述不算授权：把 ``workspace.write`` 声明成 ``auto`` 必须被基线压回 routine。"""
    from app.agents.capabilities.catalog.tool_registry import risk_tier_of
    from app.agents.skills.capability import ToolCapability

    item = ToolCapability(name="workspace_write", environment="client", write_op=True, risk_tier="auto")
    assert risk_tier_of("workspace_write", {}, item) == "routine"


def test_declaration_cannot_loosen_delete_or_git(derived):
    """``git.operations`` 基线是 C 档：声明 routine 也压不回 B 档。"""
    from app.agents.capabilities.catalog.tool_registry import risk_tier_of

    assert risk_tier_of("workspace_commit", {}) == "routine"  # 工具级例外（可信数据）
    assert risk_tier_of("git_force_push", {}) is None  # 遗留名：静态词表负责
    assert risk_tier_of("workspace_rollback", {}) == "critical"


def test_no_baseline_declaration_becomes_the_answer(derived, demo_tool):
    """全新能力没有任何基线时，声明是唯一信息源；连声明都没有才判 C 档。"""
    from app.agents.capabilities.catalog.tool_registry import risk_tier_of

    demo_tool("plugin_declared_routine", risk_tier="routine")
    assert risk_tier_of("plugin_declared_routine", {}) == "routine"
    demo_tool("plugin_silent_unknown")
    assert risk_tier_of("plugin_silent_unknown", {}) == "critical", "不认识的东西不能默认自动执行"


def test_declared_tool_is_disclosed_separately_not_as_regression(derived, demo_tool):
    """声明带来的档位差异必须**单独披露**，不能混进 switch_safe 判定。

    静态词表结构上表达不了声明，因此这类差异是有意的；把它算成"派生跟静态漂移"
    会让开关永远切不了，或者逼着人去把声明删掉。
    """
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_DECLARED_DIMENSION,
        shadow_compare,
        shadow_parity_totals,
    )

    demo_tool("plugin_declared_critical", risk_tier="critical")
    diffs = shadow_compare()
    declared_rows = diffs.get(SHADOW_DECLARED_DIMENSION) or []
    assert any(row[0] == "plugin_declared_critical" for row in declared_rows)
    assert all(row[0] != "plugin_declared_critical" for row in diffs["tool→risk_tier"])
    totals = shadow_parity_totals(diffs)
    assert totals["switch_safe"] is True
    assert totals["declared_total"] >= 1


def test_approval_engine_sees_provider_declaration(derived):
    """端到端：Provider 声明 C 档 → 审批引擎必须要求确认（即使"帮我确认"已开启）。"""
    from app.agents.skills.approval_policy import APPROVAL_MODE_AUTO, classify_tool_risk
    from app.agents.skills.capability import ToolCapability

    item = ToolCapability(name="workspace_stage_write", environment="client", risk_tier="critical")
    tier, _risk, _reason = classify_tool_risk("workspace_stage_write", {}, item)
    assert tier == "critical"

    from app.agents.skills.approval_policy import should_confirm

    decision = should_confirm(
        tool="workspace_stage_write",
        arguments={"path": "src/main.py"},
        workspace_context={"workspace_available": True, "approval_mode": APPROVAL_MODE_AUTO},
        capability=item,
    )
    assert decision.decision == "require_confirmation"
    assert decision.tier == "critical"


def test_declared_capability_drives_the_whole_chain(derived, demo_tool):
    """**声明一次 → 全链路派生**：能力归属 → Provider 路由 → MCP 目标 → 意图 → 审批。

    这是 (A) 的核心断言：新工具只要声明 ``capability``，就不必回来改
    ``TOOL_CAPABILITY_MAP`` / ``CAPABILITY_TOOL_MAP`` / ``ACTION_TOOL_WINDOW``。
    """
    from app.agents.capabilities.catalog.tool_registry import (
        _provider_for_capability,
        capability_of,
        entries_by_name,
        risk_tier_of,
    )

    demo_tool("plugin_reader_tool", capability="workspace.read")
    entry = entries_by_name()["plugin_reader_tool"]
    assert entry.capability == "workspace.read"
    assert "capability:declared" in entry.sources
    # Provider 路由/执行环境/动作类型/审批全部由能力派生。
    assert entry.provider_id == _provider_for_capability("workspace.read")
    assert entry.data_locality == "local_only"
    assert entry.requires_lease is True
    assert entry.action_type == "read"
    assert "READ" in entry.action_intents
    assert risk_tier_of("plugin_reader_tool", {}) == "auto"
    # 开关打开时 `capability_of` 也走派生结果。
    assert capability_of("plugin_reader_tool") == "workspace.read"


def test_static_table_wins_over_declared_capability(derived, demo_tool):
    """**声明不能改写既有归属**：否则一个插件就能把 ``workspace_commit`` 说成只读。

    路由/租约/审批都建立在静态表上，所以这条优先级是安全边界。
    """
    from app.agents.capabilities.catalog.tool_registry import entries_by_name

    demo_tool("workspace_commit", capability="workspace.read")
    entry = entries_by_name()["workspace_commit"]
    assert entry.capability == "git.operations"
    assert "capability:static" in entry.sources


def test_invalid_declared_capability_is_ignored(demo_tool):
    """非法能力名（非点分/含大写/奇怪字符）一律视为未声明。"""
    from app.agents.capabilities.catalog.tool_registry import declared_capability_of

    demo_tool("plugin_bad_cap_1", capability="Workspace.Read")
    assert declared_capability_of("plugin_bad_cap_1") == ""
    demo_tool("plugin_bad_cap_2", capability="notacapability")
    assert declared_capability_of("plugin_bad_cap_2") == ""
    demo_tool("plugin_bad_cap_3", capability="workspace read")
    assert declared_capability_of("plugin_bad_cap_3") == ""
    demo_tool("plugin_good_cap", capability="workspace.read@2")
    assert declared_capability_of("plugin_good_cap") == "workspace.read"


def test_declared_new_capability_enters_the_action_window(derived, demo_tool, catalog_with):
    """新能力（目录里新增描述符）+ 声明它的工具 → **自动进入动作窗口**。

    静态表（``ACTION_TOOL_WINDOW``）里没有这个能力，因此这属于"声明带来的补位"，
    单独披露、不计入 ``switch_safe``；既有窗口逐条不变。
    """
    from lumi_contracts.plugins import CapabilityDescriptor, DataLocality, SideEffectKind

    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_DECLARED_WINDOW_DIMENSION,
        shadow_compare,
        shadow_parity_totals,
    )
    from app.agents.capabilities.catalog.tool_registry import (
        action_window,
        action_window_declared,
    )

    catalog_with(
        CapabilityDescriptor(
            name="demo.publish",
            side_effects=[SideEffectKind.EXTERNAL],
            data_locality=DataLocality.CLOUD,
        )
    )
    demo_tool("plugin_publisher", capability="demo.publish")
    # 契约的 external ↔ 意图表的 publish 必须桥接上，否则这里会是空。
    assert action_window_declared("PUBLISH") == ("plugin_publisher",)
    assert action_window_declared("SEND") == ("plugin_publisher",)
    assert "plugin_publisher" in action_window("PUBLISH", fallback=("send_email",))
    # 只读意图不受影响（副作用交集为空）。
    assert action_window_declared("READ") == ()

    diffs = shadow_compare()
    assert any(row[0] == "PUBLISH" for row in diffs.get(SHADOW_DECLARED_WINDOW_DIMENSION) or [])
    assert all(row[0] != "PUBLISH" for row in diffs["intent→tool_window"])
    totals = shadow_parity_totals(diffs)
    assert totals["switch_safe"] is True
    assert totals["declared_total"] >= 1


def test_known_capabilities_never_enter_the_declared_supplement():
    """既有能力（含不在意图表里的 ``git.operations``/``artifact.create``）不得被补位。

    否则 CREATE 窗口会多出 ``create_office_document``，对拍立刻出现"回归"假象。
    """
    from app.agents.capabilities.catalog.tool_registry import (
        _known_intent_capabilities,
        action_window_declared,
    )

    known = _known_intent_capabilities()
    assert {"git.operations", "artifact.create", "code.execute"} <= set(known)
    for intent in ("READ", "CREATE", "MODIFY", "DELETE", "MOVE", "EXECUTE", "SEND", "PUBLISH"):
        assert action_window_declared(intent) == (), intent


def test_manifest_declarations_map_to_tier_and_confirmation():
    """``PluginManifest`` 的声明 → 档位/需确认：插件在 Manifest 里声明一次就生效。

    这是 (A) 的第三条渠道：类属性（工具自己）/ 能力对象（Provider 运行时）/
    Manifest（安装期自述）三者共用同一张档位词表。
    """
    from lumi_contracts.plugins import (
        PluginKind,
        PluginManifest,
        PluginPermission,
        SideEffectKind,
    )

    from app.agents.capabilities.catalog.tool_registry import (
        manifest_requires_local_confirmation,
        manifest_tier_of,
    )

    def _manifest(*, side_effects=(), permissions=()):
        return PluginManifest(
            id="demo.plugin",
            version="1.0.0",
            kind=PluginKind.CAPABILITY_PROVIDER,
            entrypoints={"capability": "workspace.read"},
            side_effects=list(side_effects),
            permissions=list(permissions),
        )

    read_only = _manifest(side_effects=[SideEffectKind.READ])
    assert manifest_tier_of(read_only) == "auto"
    assert manifest_requires_local_confirmation(read_only) is False

    writer = _manifest(side_effects=[SideEffectKind.WRITE])
    assert manifest_tier_of(writer) == "routine"
    assert manifest_requires_local_confirmation(writer) is True

    # external（对外发布/发送）不可逆 → C 档。
    publisher = _manifest(side_effects=[SideEffectKind.EXTERNAL])
    assert manifest_tier_of(publisher) == "critical"

    # 只声明"本机需确认"的权限也要落地：读取能力不能因此静默自动执行。
    guarded = _manifest(
        side_effects=[SideEffectKind.READ],
        permissions=[PluginPermission(name="workspace.read", needs_local_confirmation=True)],
    )
    assert manifest_requires_local_confirmation(guarded) is True
    assert manifest_tier_of(guarded) == "routine"

    # 什么都没声明 → 不猜（档位由它提供的能力描述符决定）。
    silent = _manifest()
    assert manifest_tier_of(silent) == ""


def test_plugin_installation_view_exposes_declared_tier():
    """安装视图必须把声明档位暴露给前端（与审批引擎同一张词表，不能两套口径）。"""
    from lumi_contracts.plugins import PluginKind, PluginManifest, SideEffectKind

    from app.plugins.registry import PluginInstallation

    installation = PluginInstallation(
        manifest=PluginManifest(
            id="demo.plugin",
            version="1.0.0",
            kind=PluginKind.CAPABILITY_PROVIDER,
            entrypoints={"capability": "workspace.write"},
            side_effects=[SideEffectKind.WRITE],
        )
    )
    payload = installation.to_api()
    assert payload["declared_risk_tier"] == "routine"
    assert payload["requires_confirmation"] is True


def test_flag_off_ignores_declarations_entirely(monkeypatch, demo_tool):
    """开关关闭时行为**逐字不变**：声明不参与判定（静态词表仍是唯一真相源）。"""
    from app.core.config import settings
    from app.agents.skills.approval_policy import classify_tool_risk, static_tier_of

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", False)
    demo_tool("plugin_declared_auto_off", risk_tier="auto")
    assert classify_tool_risk("plugin_declared_auto_off", {}) == static_tier_of(
        "plugin_declared_auto_off", {}
    )
