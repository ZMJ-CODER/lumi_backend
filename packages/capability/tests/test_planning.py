"""工具窗口计划算法的纯函数测试（词表与查询全部注入）。"""

from __future__ import annotations

from dataclasses import dataclass

from lumi_capability import planning as p

READ = "resource.read"
WRITE = "resource.write"
DELETE = "resource.delete"

INTENTS = {"READ": READ, "SEARCH": READ, "CREATE": WRITE, "DELETE": DELETE}
TOOLS = {
    (READ, "workspace"): "workspace_navigator",
    (READ, "office_document"): "office_doc_read",
    (WRITE, "workspace"): "workspace_write",
    (DELETE, "workspace"): "workspace_delete",
}
PROVIDERS = {
    (READ, "workspace"): ("workspace_provider",),
    (WRITE, "workspace"): ("workspace_provider",),
    (DELETE, "workspace"): ("workspace_provider",),
}
CANDIDATES = {
    (READ, "workspace"): ("workspace_navigator",),
    (WRITE, "workspace"): ("workspace_write",),
    (DELETE, "workspace"): ("workspace_delete",),
    (READ, "office_document"): ("office_doc_read",),
}
BINDINGS = {
    "workspace_navigator": (READ, "workspace"),
    "workspace_write": (WRITE, "workspace"),
    "workspace_delete": (DELETE, "workspace"),
    "office_doc_read": (READ, "office_document"),
}


@dataclass
class _Binding:
    known: bool
    capability: str = ""
    resource_type: str = ""


def _facts() -> p.WindowPlanning:
    return p.WindowPlanning(
        intent_capability=INTENTS,
        canonical_tools=TOOLS,
        mutating_capabilities=frozenset({WRITE, DELETE}),
        read_first_intents=frozenset({"DELETE"}),
        read_capability=READ,
        candidates_for=lambda capability, resource_type: CANDIDATES.get((capability, resource_type), ()),
        providers_for=lambda capability, resource_type: PROVIDERS.get((capability, resource_type), ()),
        resource_types_from_tools=lambda tools: tuple(
            dict.fromkeys(
                BINDINGS[name][1] for name in tools if name in BINDINGS
            )
        ),
    )


# ── 1. 规范工具选择 ─────────────────────────────────────────


def test_seed_table_wins_when_in_candidates():
    tool = p.canonical_tool_for(READ, "workspace", canonical_tools=TOOLS, candidates=("workspace_navigator",))
    assert tool == "workspace_navigator"


def test_seed_falls_back_to_first_candidate_when_not_registered():
    """种子工具不在候选池里（未注册/被过滤）时用候选里的第一个，**绝不**给不存在的名字。"""
    tool = p.canonical_tool_for(
        READ, "workspace", canonical_tools=TOOLS, candidates=("read", "other_reader")
    )
    assert tool == "read"
    # 种子在池里时按种子走（哪怕它不是第一个）
    assert p.canonical_tool_for(
        READ, "workspace", canonical_tools=TOOLS, candidates=("read", "workspace_navigator")
    ) == "workspace_navigator"


def test_no_candidate_and_no_seed_returns_empty():
    assert p.canonical_tool_for(READ, "knowledge", canonical_tools=TOOLS) == ""
    assert p.canonical_tool_for(READ, "knowledge", canonical_tools=TOOLS, candidates=()) == ""


def test_unknown_capability_with_candidates_uses_candidate():
    """新 Provider 的新能力：表里没有，但候选池里有 → 用它（"新增 Provider 不改表"）。"""
    tool = p.canonical_tool_for("resource.publish", "workspace", canonical_tools=TOOLS, candidates=("publish",))
    assert tool == "publish"


# ── 2. 计划 ─────────────────────────────────────────────────


def test_plan_derives_tools_and_providers():
    plan = p.plan_window(["CREATE"], planning=_facts(), resource_types=["workspace"])
    assert plan.capabilities == (WRITE,)
    assert plan.derived == ("workspace_write",)
    assert plan.tools_by_capability == (("CREATE", WRITE, "workspace_write"),)
    assert plan.providers_by_capability == ((WRITE, "workspace", ("workspace_provider",)),)
    assert plan.source == "resource"


def test_unknown_intent_is_ignored_and_source_stays_static():
    plan = p.plan_window(["SEND"], planning=_facts(), resource_types=["workspace"])
    assert plan.capabilities == () and plan.derived == () and plan.pinned_reads == ()
    assert plan.source == "static"


def test_mutating_intent_pins_the_read_entry():
    """先读才能改：写/删在场时，同资源的读入口必须被钉住。"""
    plan = p.plan_window(["DELETE"], planning=_facts(), resource_types=["workspace"])
    assert plan.pinned_reads == ("workspace_navigator",)


def test_read_only_intent_pins_nothing():
    plan = p.plan_window(["READ"], planning=_facts(), resource_types=["workspace"])
    assert plan.pinned_reads == ()


def test_resource_types_fall_back_to_window_inference():
    """没有资源类型时从旧窗口反推（保守：只认已绑定的工具）。"""
    plan = p.plan_window(["CREATE"], planning=_facts(), fallback=["workspace_write"])
    assert plan.resource_types == ("workspace",)
    assert "workspace_write" in plan.derived


def test_intents_are_normalized_and_deduped():
    plan = p.plan_window(["create", " CREATE ", None, ""], planning=_facts(), resource_types=["workspace"])
    assert plan.intents == ("CREATE",)


def test_plan_as_dict_shape_is_stable():
    plan = p.plan_window(["CREATE"], planning=_facts(), resource_types=["workspace"])
    payload = plan.as_dict()
    assert set(payload) == {
        "intents", "capabilities", "resource_types", "tools_by_capability",
        "providers_by_capability", "pinned_reads", "derived", "fallback", "source",
    }
    assert payload["tools_by_capability"] == [
        {"intent": "CREATE", "capability": WRITE, "tool": "workspace_write"}
    ]


# ── 3. 窗口（顺序即契约）────────────────────────────────────


def test_window_never_loses_the_old_tools():
    """窗口只增不减：旧窗口永远在最前面。"""
    plan = p.plan_window(["CREATE"], planning=_facts(), resource_types=["workspace"], fallback=["legacy_tool"])
    names = p.plan_names(plan, read_first_intents=frozenset({"DELETE"}))
    assert names[0] == "legacy_tool"
    assert set(names) >= {"legacy_tool", "workspace_write"}
    assert len(names) == len(set(names)), "必须去重"


def test_read_entry_is_moved_to_front_for_read_first_intents():
    plan = p.plan_window(["DELETE"], planning=_facts(), resource_types=["workspace"], fallback=["workspace_delete"])
    names = p.plan_names(plan, read_first_intents=frozenset({"DELETE"}))
    assert names[0] == "workspace_navigator"
    assert set(names) == {"workspace_navigator", "workspace_delete"}


def test_read_first_does_not_reorder_plain_reads():
    plan = p.plan_window(["READ"], planning=_facts(), resource_types=["workspace"], fallback=["workspace_navigator"])
    assert p.plan_names(plan, read_first_intents=frozenset({"DELETE"})) == ("workspace_navigator",)


# ── 4. 截断保护 ─────────────────────────────────────────────


def _binding_for(name: str) -> _Binding:
    if name not in BINDINGS:
        return _Binding(known=False)
    capability, resource_type = BINDINGS[name]
    return _Binding(known=True, capability=capability, resource_type=resource_type)


def _read_tool_for(resource_type: str) -> str:
    return CANDIDATES.get((READ, resource_type), ("",))[0]


def test_guard_pins_read_entry_of_mutated_resource():
    guards = p.read_guards(
        ["workspace_write", "workspace_navigator"],
        binding_for=_binding_for,
        read_tool_for=_read_tool_for,
        mutating_capabilities={WRITE, DELETE},
    )
    assert guards == frozenset({"workspace_navigator"})


def test_guard_is_empty_without_mutating_tools():
    guards = p.read_guards(
        ["workspace_navigator"], binding_for=_binding_for, read_tool_for=_read_tool_for,
        mutating_capabilities={WRITE, DELETE},
    )
    assert guards == frozenset()


def test_guard_never_pins_a_name_outside_the_pool():
    """钉一个不在池里的名字会变成 dropped_core 噪音（故障信号不能滥用）。"""
    guards = p.read_guards(
        ["workspace_write"], binding_for=_binding_for, read_tool_for=_read_tool_for,
        mutating_capabilities={WRITE, DELETE},
    )
    assert guards == frozenset()


def test_guard_ignores_unbound_tools():
    guards = p.read_guards(
        ["mystery_tool", "workspace_write", "workspace_navigator"],
        binding_for=_binding_for, read_tool_for=_read_tool_for, mutating_capabilities={WRITE, DELETE},
    )
    assert guards == frozenset({"workspace_navigator"})


def test_guard_accepts_objects_with_name_attribute():
    @dataclass
    class _Item:
        name: str = "workspace_write"

    guards = p.read_guards(
        [_Item(), "workspace_navigator"],
        binding_for=_binding_for, read_tool_for=_read_tool_for, mutating_capabilities={WRITE, DELETE},
    )
    assert guards == frozenset({"workspace_navigator"})
