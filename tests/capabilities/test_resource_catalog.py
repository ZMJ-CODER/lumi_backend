"""统一资源能力层 Phase 1（兼容能力目录）回归。

验收标准（方案 Phase 1）：**Read、workspace_navigator、office_doc_read 都要映射到
``resource.read```**；同时这一阶段**不改任何执行路径**，因此还要钉住"零行为变化"。

四组断言：

1. 验收 + 映射完备性（旧的九张能力声明全部有绑定；未绑定的工具必须**看得见**）；
2. 词汇与 Provider 声明自洽（统一能力/资源类型封闭；Provider 只声明不路由）；
3. 三态可见性（注册 / 可见 / 可用，且"系统不认识"与"暂时调不到"不混为一谈）；
4. 零行为变化（影子对拍仍为空；档位/能力查询逐字不变）。
"""

from __future__ import annotations

import pytest

from app.agents.capabilities.catalog import resource as rc


# ── 1. 验收 + 映射完备性 ────────────────────────────────────


def test_phase1_acceptance_read_tools_map_to_resource_read():
    """验收标准：三种"读"的工具名都要落到同一个统一能力上。"""
    for tool in ("Read", "workspace_navigator", "office_doc_read"):
        binding = rc.binding_for_tool(tool)
        assert binding.capability == rc.UNIFIED_RESOURCE_READ, tool
        assert binding.known is True, tool
        assert binding.resource_type in rc.RESOURCE_TYPES, tool
    # 资源类型必须区分开：工作区读取与办公文档读取是两种资源，
    # 否则 Broker（Phase 3）就没法按资源类型选 Provider。
    assert rc.binding_for_tool("workspace_navigator").resource_type == rc.RESOURCE_WORKSPACE
    assert rc.binding_for_tool("office_doc_read").resource_type == rc.RESOURCE_OFFICE_DOCUMENT


def test_acceptance_via_registry_derived_capability():
    """注册表派生/插件声明的能力也要能落进统一层（Phase 1 的兼容入口）。"""
    # 调用方显式传入旧能力名（注册表条目构造就是这么做的）
    binding = rc.binding_for_tool("mcp__lumi_client__workspace_write", legacy_capability="workspace.write")
    assert (binding.capability, binding.resource_type) == (rc.UNIFIED_RESOURCE_WRITE, rc.RESOURCE_WORKSPACE)
    # 新的工具声明可以直接给统一能力名
    declared = rc.binding_for_tool("memory_write", legacy_capability="resource.write", resource_type="memory")
    assert declared.capability == rc.UNIFIED_RESOURCE_WRITE
    assert declared.resource_type == "memory"


def test_every_catalog_capability_has_a_binding():
    """九张既有能力声明**一个都不能漏**（漏一个就是"新层里没有这个能力"）。"""
    from app.agents.capabilities.catalog.legacy import capability_catalog

    for descriptor in capability_catalog.all():
        assert descriptor.name in rc.LEGACY_CAPABILITY_BINDINGS, f"缺少统一层绑定：{descriptor.name}"
    # 反向：映射表里不该有目录之外的能力名（否则是拼错或已删除）
    known = set(capability_catalog.names())
    assert set(rc.LEGACY_CAPABILITY_BINDINGS) == known


def test_all_bound_tools_are_self_consistent():
    """每条绑定都必须落在封闭词汇里，且工具级例外也要自洽。"""
    for tool, (capability, resource_type) in rc.TOOL_BINDINGS.items():
        assert capability in rc.UNIFIED_CAPABILITIES, tool
        assert resource_type in rc.RESOURCE_TYPES, tool
    for legacy, (capability, resource_type) in rc.LEGACY_CAPABILITY_BINDINGS.items():
        assert capability in rc.UNIFIED_CAPABILITIES, legacy
        assert resource_type in rc.RESOURCE_TYPES, legacy


def test_tools_with_a_capability_are_all_bound():
    """静态表里**有能力**的工具必须全部绑定；没有能力的本机动作允许未绑定但要看得见。"""
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    unbound: list[str] = []
    for tool, capability in TOOL_CAPABILITY_MAP.items():
        binding = rc.binding_for_tool(tool, legacy_capability=str(capability or ""))
        if not capability:
            continue
        if not binding.known:
            unbound.append(tool)
    assert unbound == [], f"这些工具还没有接入统一资源能力层：{unbound}"


def test_local_actions_without_capability_are_visible_not_silent():
    """没有能力的本机动作（打开 URL 等）要出现在未绑定清单里，而不是被悄悄忽略。"""
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    no_capability = [tool for tool, capability in TOOL_CAPABILITY_MAP.items() if not capability]
    assert no_capability, "本机动作应当仍然存在（用于证明'没有能力'这条分支被覆盖）"
    unknown = rc.unbound_tools([*TOOL_CAPABILITY_MAP, "totally_unknown_tool"])
    assert "totally_unknown_tool" in unknown
    assert set(unknown) <= set(TOOL_CAPABILITY_MAP) | {"totally_unknown_tool"}


def test_unknown_tool_never_guesses():
    """不认识就不猜：返回空绑定，而不是"名字里有 write 就当写"。"""
    for tool in ("workspace_write_extra", "super_writer", "not_a_tool", ""):
        binding = rc.binding_for_tool(tool)
        assert binding.known is False, tool
        assert binding.source == "unknown", tool
    assert rc.unbound_tools(["", "a", "a"]) == ["a"]


def test_unbound_tools_is_sorted_and_unique():
    assert rc.unbound_tools(["b", "a", "b", "", "c"]) == ["a", "b", "c"]


# ── 2. 词汇与 Provider 声明 ─────────────────────────────────


def test_unbound_static_tools_are_all_in_a_deferred_family():
    """边界必须**被验证**：未接入的静态工具要么已绑定，要么明确属于某个"刻意推迟"的族。

    这条断言是 Phase 1 的价值所在——它把"还没接入"从含糊的现状变成长短可数的清单：
    新增一个工具却忘了登记，测试会直接指出来。
    """
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP
    from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP

    universe = set(TOOL_CAPABILITY_MAP) | set(IMPLEMENTATION_MAP)
    missing = [
        tool
        for tool in sorted(universe)
        if not rc.binding_for_tool(tool).known and not rc.deferred_family_of(tool)
    ]
    assert missing == [], f"这些工具既没接入也没登记到 deferred 族：{missing}"
    # 三个族都要真的有内容（空族意味着归类已经过期）
    for family, names in rc.DEFERRED_TOOL_FAMILIES.items():
        assert names, family


def test_deferred_family_lookup_is_name_shape_tolerant():
    assert rc.deferred_family_of("mcp__lumi_client__web_search") == "external_service"
    assert rc.deferred_family_of("Web_Search") == "external_service"
    assert rc.deferred_family_of("workspace_write") == ""


def test_knowledge_read_tool_exists_for_the_declared_provider():
    """知识 Provider 虽然只有声明，但它的读取入口必须已经在统一层里（否则是空壳）。"""
    binding = rc.binding_for_tool("query_knowledge")
    assert binding.capability == rc.UNIFIED_RESOURCE_READ
    assert binding.resource_type == rc.RESOURCE_KNOWLEDGE
    assert binding.provider == "knowledge_provider"


def test_unified_vocabulary_is_closed():
    assert rc.is_unified_capability("resource.read") is True
    assert rc.is_unified_capability("sandbox.run") is True, "别名也算统一能力"
    assert rc.is_unified_capability("workspace.read") is False, "旧能力名不是统一能力"
    assert rc.is_unified_capability("nonsense") is False
    assert rc.normalize_unified_capability("sandbox.run") == rc.UNIFIED_CODE_EXECUTE
    assert rc.normalize_unified_capability("resource.search") == rc.UNIFIED_RESOURCE_READ
    assert rc.normalize_unified_capability("nonsense") == "nonsense"


def test_resource_types_are_declared_and_providers_cover_them():
    """每个资源类型都要有 Provider 声明（哪怕是"只有声明、还没实现"）。"""
    covered = {rtype for spec in rc.RESOURCE_PROVIDERS for rtype in spec.resource_types}
    assert rc.RESOURCE_TYPES <= covered
    for spec in rc.RESOURCE_PROVIDERS:
        for capability in spec.capabilities:
            assert capability in rc.UNIFIED_CAPABILITIES, spec.name
        for rtype in spec.resource_types:
            assert rtype in rc.RESOURCE_TYPES, spec.name


def test_workspace_write_has_two_candidate_providers_in_order():
    """同一 (能力, 资源类型) 可以有多个 Provider：候选**有序**，选择权在 Broker。"""
    candidates = rc.providers_for("resource.write", rc.RESOURCE_WORKSPACE)
    names = [spec.name for spec in candidates]
    assert names == ["workspace_provider", "git_provider"], names
    assert rc.providers_for("resource.delete", rc.RESOURCE_ARTIFACT) == ()
    assert rc.providers_for("", rc.RESOURCE_WORKSPACE) == ()
    assert rc.providers_for("resource.read", "") == ()


def test_provider_specs_expose_execution_env_and_registration():
    """Provider 声明要能回答"跑在哪一侧""有没有实现"——前端据此后置状态。"""
    workspace = rc.PROVIDERS_BY_NAME["workspace_provider"]
    assert workspace.execution_env == "client"
    assert workspace.registered is True
    assert workspace.provider_id == "lumi.local.workspace"
    artifact = rc.PROVIDERS_BY_NAME["artifact_provider"]
    assert (artifact.execution_env, artifact.provider_id) == ("server", "lumi.server.artifact")
    # 两代对照缺口 2b：代码 Provider 的**工作侧**是客户端（真实租约 deployment=client /
    # plane=client）；"沙箱"是运行方式，属于租约的 runtime_kind，不是这个字段。
    code = rc.PROVIDERS_BY_NAME["code_provider"]
    assert code.execution_env == "client"
    assert "沙箱" in code.note
    # 知识库还没有 Provider 实现：**明确标成未注册**，而不是假装能用
    knowledge = rc.PROVIDERS_BY_NAME["knowledge_provider"]
    assert knowledge.registered is False
    assert knowledge.provider_id == ""
    assert rc.DEFAULT_PROVIDER_BY_RESOURCE[rc.RESOURCE_KNOWLEDGE] == "knowledge_provider"


def test_execution_env_only_ever_answers_which_side_serves_the_capability():
    """"工作侧"这一维只允许 client / server。

    两代对照缺口 2b 的根因就是两个维度被混用：``sandbox`` 描述的是**运行方式**
    （租约的 ``runtime_kind``），把它写进"工作侧"会让管理端显示"代码能力跑在沙箱侧"，
    而 Broker 实际按客户端租约派发。这条断言把词表钉死，防止再混回来。
    """
    for spec in rc.RESOURCE_PROVIDERS:
        assert spec.execution_env in {"client", "server"}, (spec.name, spec.execution_env)


def test_native_action_family_comes_from_the_old_routing_table():
    """缺口 2d：本机动作是旧代写下来的**决定**，新层必须继承它而不是降级成 unknown。"""
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    declared = {name for name, capability in TOOL_CAPABILITY_MAP.items() if not capability}
    assert declared, "旧路由表里应当仍有显式的本机动作条目"
    assert rc.native_action_tools() == frozenset(declared)
    # 名单与族分类同源：新增一个本机动作却忘了登记族，这条会红。
    assert {name.casefold() for name in rc.DEFERRED_TOOL_FAMILIES["native_action"]} == {
        name.casefold() for name in rc.native_action_tools()
    }
    for tool in ("desktop_open_app", "desktop_open_url", "user_clarify"):
        assert rc.is_native_action(tool) is True, tool
        assert rc.deferred_family_of(tool) == "native_action", tool
    # 归一化写法同样认（模型看到的是 mcp__… 形式）
    assert rc.deferred_family_of("mcp__lumi_pc__user_clarify") == "native_action"
    assert rc.is_native_action("workspace_write") is False
    # 本机动作仍然是"未绑定"（它确实不接资源层），只是不再含糊地叫 unknown
    assert "desktop_open_url" in rc.unbound_tools(["desktop_open_url"])


def test_native_actions_are_not_reported_as_server_side():
    """本机动作在**客户端**执行——不能因为"没有能力名"就默认成服务端执行。"""
    from app.agents.capabilities.catalog.tool_registry import ENV_CLIENT, resolve_tool

    for tool in ("desktop_open_app", "desktop_open_url", "user_clarify"):
        entry = resolve_tool(tool)
        assert entry is not None, tool
        assert entry.capability == "", tool
        assert entry.execution_env == ENV_CLIENT, tool


def test_catalog_snapshot_is_json_safe():
    snapshot = rc.catalog_snapshot()
    assert set(snapshot) >= {
        "unified_capabilities", "aliases", "resource_types",
        "legacy_bindings", "tool_bindings", "providers", "default_provider_by_resource",
        # 缺口 2d：管理端要能把"本机动作"与"漏接的工具"分开显示。
        "native_action_tools", "deferred_families",
    }
    assert snapshot["native_action_tools"] == sorted(rc.native_action_tools())
    assert set(snapshot["deferred_families"]) == set(rc.DEFERRED_TOOL_FAMILIES)
    assert snapshot["unified_capabilities"] == sorted(rc.UNIFIED_CAPABILITIES)
    assert snapshot["legacy_bindings"]["workspace.read"] == {
        "capability": rc.UNIFIED_RESOURCE_READ,
        "resource_type": rc.RESOURCE_WORKSPACE,
    }
    assert len(snapshot["providers"]) == len(rc.RESOURCE_PROVIDERS)
    import json

    json.dumps(snapshot, ensure_ascii=False)


# ── 3. 三态可见性 ───────────────────────────────────────────


def test_visibility_distinguishes_unregistered_from_unavailable():
    """"系统不认识"与"有但此刻调不到"必须分开（否则模型会去编替代做法）。"""
    from app.agents.skills.capability import ToolCapability

    assert rc.resource_visibility("totally_unknown_tool") == rc.STATE_UNREGISTERED
    assert rc.resource_visibility("workspace_write") == rc.STATE_REGISTERED
    eligible = ToolCapability(name="workspace_write", environment="client")
    assert rc.resource_visibility("workspace_write", capability=eligible) == rc.STATE_VISIBLE
    offline = ToolCapability(
        name="workspace_write",
        environment="client",
        annotations={"availability_hint": "offline"},
    )
    assert rc.resource_visibility("workspace_write", capability=offline) == rc.STATE_UNAVAILABLE
    healthy = ToolCapability(
        name="workspace_write",
        environment="client",
        annotations={"availability_hint": "available"},
    )
    assert rc.resource_visibility("workspace_write", capability=healthy) == rc.STATE_AVAILABLE


def test_visibility_never_reports_available_for_unknown_tool():
    from app.agents.skills.capability import ToolCapability

    state = rc.resource_visibility(
        "totally_unknown_tool",
        capability=ToolCapability(name="totally_unknown_tool", annotations={"availability_hint": "available"}),
    )
    assert state == rc.STATE_UNREGISTERED


# ── 4. 零行为变化（Phase 1 只加元数据）────────────────────────


def test_registry_entries_carry_resource_metadata():
    from app.agents.capabilities.catalog.tool_registry import entries_by_name

    entries = entries_by_name()
    write = entries["workspace_write"]
    assert write.unified_capability == rc.UNIFIED_RESOURCE_WRITE
    assert write.resource_type == rc.RESOURCE_WORKSPACE
    assert write.resource_provider == "workspace_provider"
    assert write.provider_candidates == ("workspace_provider", "git_provider")
    # 工具级例外：git.operations 里的 diff 是只读
    assert entries["workspace_diff"].unified_capability == rc.UNIFIED_RESOURCE_READ
    # 产物落在产物目录：资源类型不是工作区
    artifact = entries["create_office_document"]
    assert artifact.unified_capability == rc.UNIFIED_ARTIFACT_CREATE
    assert artifact.resource_type == rc.RESOURCE_ARTIFACT
    assert artifact.resource_provider == "artifact_provider"
    # 本机动作没有能力 → 没有资源绑定（不猜）
    assert entries["desktop_open_url"].unified_capability == ""
    payload = write.as_dict()
    for key in ("unified_capability", "resource_type", "resource_provider", "provider_candidates"):
        assert key in payload, key


def test_phase1_changes_no_behavior():
    """Phase 1 只加元数据：影子对拍的**判定维度**仍为零差异，档位/能力查询逐字不变。

    披露维度（声明档位、声明窗口补位、资源层窗口）可以非空——它们是"打开开关后
    会变成什么"的预览，**有意为之**，不参与 ``switch_safe``。
    """
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP, capability_for_tool
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_PARITY_DIMENSIONS,
        shadow_compare,
        shadow_parity_totals,
    )
    from app.agents.capabilities.catalog.tool_registry import (
        capability_of,
    )
    from app.agents.skills.approval_policy import static_tier_of

    diffs = shadow_compare()
    totals = shadow_parity_totals(diffs)
    assert totals["switch_safe"] is True, diffs
    for dimension in SHADOW_PARITY_DIMENSIONS:
        assert diffs.get(dimension) == [], (dimension, diffs.get(dimension))

    for tool in TOOL_CAPABILITY_MAP:
        assert capability_of(tool) == (capability_for_tool(tool) or ""), tool
    assert static_tier_of("workspace_write", {})[0] == "routine"
    assert static_tier_of("workspace_diff", {})[0] == "auto"


def test_binding_failure_does_not_break_entry_construction():
    """元数据派生失败绝不能影响条目构造（观测/兼容层不该有"能拖垮主流程"的权力）。"""
    from app.agents.capabilities.catalog import tool_registry as registry

    original = rc.binding_for_tool

    def _boom(*_args, **_kwargs):
        raise RuntimeError("catalog exploded")

    rc.binding_for_tool = _boom
    try:
        entry = registry._entry_from_static_only("workspace_write", meta={})
        assert entry.capability == "workspace.write"
        assert entry.unified_capability == ""
    finally:
        rc.binding_for_tool = original


@pytest.mark.parametrize(
    ("tool", "capability", "resource_type"),
    [
        ("workspace_navigator", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
        ("workspace_write", rc.UNIFIED_RESOURCE_WRITE, rc.RESOURCE_WORKSPACE),
        ("workspace_edit", rc.UNIFIED_RESOURCE_EDIT, rc.RESOURCE_WORKSPACE),
        ("workspace_move", rc.UNIFIED_RESOURCE_MOVE, rc.RESOURCE_WORKSPACE),
        ("workspace_delete", rc.UNIFIED_RESOURCE_DELETE, rc.RESOURCE_WORKSPACE),
        ("workspace_commit", rc.UNIFIED_RESOURCE_WRITE, rc.RESOURCE_WORKSPACE),
        ("workspace_diff", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
        ("workspace_code_scan", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
        ("sandbox_run", rc.UNIFIED_CODE_EXECUTE, rc.RESOURCE_WORKSPACE),
        ("office_doc_read", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_OFFICE_DOCUMENT),
        ("office_doc_edit", rc.UNIFIED_RESOURCE_WRITE, rc.RESOURCE_OFFICE_DOCUMENT),
        ("create_office_document", rc.UNIFIED_ARTIFACT_CREATE, rc.RESOURCE_ARTIFACT),
        ("Read", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
        ("Write", rc.UNIFIED_RESOURCE_WRITE, rc.RESOURCE_WORKSPACE),
        ("Edit", rc.UNIFIED_RESOURCE_EDIT, rc.RESOURCE_WORKSPACE),
        ("Rename", rc.UNIFIED_RESOURCE_MOVE, rc.RESOURCE_WORKSPACE),
        ("Delete", rc.UNIFIED_RESOURCE_DELETE, rc.RESOURCE_WORKSPACE),
        ("Bash", rc.UNIFIED_CODE_EXECUTE, rc.RESOURCE_WORKSPACE),
        ("Glob", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
        ("Grep", rc.UNIFIED_RESOURCE_READ, rc.RESOURCE_WORKSPACE),
    ],
)
def test_binding_matrix(tool, capability, resource_type):
    """绑定矩阵：把"哪个工具属于哪种资源上的哪个操作"一次性钉住。"""
    binding = rc.binding_for_tool(tool)
    assert binding.capability == capability
    assert binding.resource_type == resource_type
