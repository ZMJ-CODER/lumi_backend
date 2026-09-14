"""统一资源能力层 Phase 2（统一预检与工具窗口）回归。

Phase 2 的验收是两句话：

1. **工具窗口由"动作意图 + 资源类型"派生**（而不是直接读 ``ACTION_TOOL_WINDOW``），
   旧表降级为 fallback；
2. **核心读取能力不被 Top-K 截断**——"新增 workspace_write 不会让 resource.read 消失"。

因此这里的断言分三层：窗口派生（含超集不变量）、截断保护（含"关闭开关时逐字不变"）、
以及对拍口径（资源层差异只增不减，属披露维度，不能混进 ``switch_safe``）。
"""

from __future__ import annotations

import pytest

from app.agents.capabilities.policy import resource_window as rw
from app.agents.orchestration.preflight.capability_preflight import ACTION_TOOL_WINDOW, tool_window_for_actions


@pytest.fixture()
def window_on(monkeypatch):
    """打开资源能力窗口（默认关闭）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", True)
    return settings


@pytest.fixture()
def window_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", False)
    return settings


def _cap(name: str):
    """最小工具能力替身（只带名字：窗口层只看名字）。"""
    from app.agents.skills.capability import ToolCapability

    return ToolCapability(name=name)


# ── 1. 窗口派生 ─────────────────────────────────────────────


def test_flag_defaults_to_off():
    from app.core.config import settings

    assert settings.RESOURCE_CAPABILITY_WINDOW is False
    assert rw.window_enabled() is False


def test_flag_off_keeps_static_window_verbatim(window_off):
    """关闭时逐字走旧路径（这是"能不能安全上线"的底线）。"""
    for intent, static in ACTION_TOOL_WINDOW.items():
        assert tool_window_for_actions([intent]) == static, intent


def test_resource_window_is_never_smaller_than_static(window_on):
    """**超集不变量**：资源层只补位、不删工具。

    否则"迁移到资源层"就会变成"某些场景下工具变少"——那是回归，不是迁移。
    """
    for intent, static in ACTION_TOOL_WINDOW.items():
        derived = tool_window_for_actions([intent])
        assert set(static) <= set(derived), (intent, static, derived)


def test_resource_window_adds_read_entry_for_mutating_intents(window_on):
    """变更类意图必须带上读取入口（先读才能改），静态表对 CREATE 漏了这一步。"""
    derived = tool_window_for_actions(["CREATE"])
    assert derived[0] == "workspace_navigator", derived
    assert "workspace_write" in derived
    # MODIFY/DELETE/MOVE 静态表本来就带读入口 → 结果不变
    for intent in ("MODIFY", "DELETE", "MOVE"):
        assert tool_window_for_actions([intent]) == ACTION_TOOL_WINDOW[intent], intent


def test_read_only_intent_window_is_unchanged(window_on):
    assert tool_window_for_actions(["READ"]) == ACTION_TOOL_WINDOW["READ"]
    assert tool_window_for_actions(["SEARCH"]) == ACTION_TOOL_WINDOW["SEARCH"]


def test_office_document_resource_gets_its_own_tools(window_on):
    """同是 CREATE，资源类型换成办公文档就该出现文档工具（这就是"按资源选 Provider"）。"""
    window = rw.resource_window(
        ["CREATE"], resource_types=["office_document"], fallback=ACTION_TOOL_WINDOW["CREATE"]
    )
    assert "office_doc_edit" in window, window
    assert "office_doc_read" in window, "变更办公文档也要带读取入口"
    # 旧窗口作为 fallback 必须保留（Phase 5 之前不删工具）
    assert "workspace_write" in window


def test_unknown_intent_falls_back_to_static(window_on):
    assert tool_window_for_actions(["NOT_AN_INTENT"]) == ()
    assert tool_window_for_actions(["SEND"]) == ACTION_TOOL_WINDOW["SEND"]
    assert tool_window_for_actions(["PUBLISH"]) == ACTION_TOOL_WINDOW["PUBLISH"]


def test_resource_types_for_profile_reads_target_scope_and_info_sources():
    assert rw.resource_types_for_profile({"target_scope": "WORKSPACE"}) == ("workspace",)
    assert rw.resource_types_for_profile({"target_scope": "ATTACHMENT"}) == ("office_document",)
    assert rw.resource_types_for_profile(
        {"info_sources": ["INTERNAL_KNOWLEDGE"]}
    ) == ("knowledge",)
    assert rw.resource_types_for_profile(
        {"info_sources": ["WORKSPACE", "ATTACHED_FILE"]}
    ) == ("workspace", "office_document")
    # 画像没给就是空（不猜），调用方回落到"从旧窗口反推"
    assert rw.resource_types_for_profile(None) == ()
    assert rw.resource_types_for_profile({}) == ()
    assert rw.resource_types_for_profile({"target_scope": "NONE"}) == ()


def test_resource_types_from_window_reverse_lookup():
    """老画像没有资源字段时，从旧窗口反推资源类型（保守：只认已绑定的工具）。"""
    assert rw.resource_types_from_window(["workspace_write"]) == ("workspace",)
    assert rw.resource_types_from_window(["office_doc_edit"]) == ("office_document",)
    assert rw.resource_types_from_window(["totally_unknown"]) == ()
    assert rw.resource_types_from_window(
        ["office_doc_edit", "workspace_write"]
    ) == ("office_document", "workspace")


def test_required_capability_table():
    assert rw.required_capability("CREATE") == rw.UNIFIED_RESOURCE_WRITE
    assert rw.required_capability("MODIFY") == rw.UNIFIED_RESOURCE_EDIT
    assert rw.required_capability("DELETE") == rw.UNIFIED_RESOURCE_DELETE
    assert rw.required_capability("MOVE") == rw.UNIFIED_RESOURCE_MOVE
    assert rw.required_capability("EXECUTE") == rw.UNIFIED_CODE_EXECUTE
    assert rw.required_capability("READ") == rw.UNIFIED_RESOURCE_READ
    # 外部资源（邮件/发布）刻意没有统一能力：宁可不派生，也不要编一个
    assert rw.required_capability("SEND") == ""
    assert rw.required_capability("PUBLISH") == ""
    assert rw.required_capability("") == ""


def test_canonical_tool_seed_then_registry():
    """种子表优先；表里没有的组合按**注册表候选**定序（新 Provider 不必改表）。"""
    assert rw.canonical_tool_for("resource.read", "workspace") == "workspace_navigator"
    # 种子工具不在候选池里 → 用候选而不是注入不存在的名字
    assert (
        rw.canonical_tool_for("resource.read", "workspace", candidates=["workspace_search"])
        == "workspace_search"
    )
    # 全新能力（Phase 6 的 memory 场景）：没有种子，用注册表候选
    assert (
        rw.canonical_tool_for("resource.write", "memory", candidates=["memory_write"])
        == "memory_write"
    )
    assert rw.canonical_tool_for("resource.write", "memory") == ""


def test_plan_explains_itself(window_on):
    """计划要能回答"这次为什么给了这些工具"（排障面板与前端展示都用它）。"""
    plan = rw.plan_for_actions(["CREATE"], resource_types=["workspace"])
    payload = plan.as_dict()
    assert payload["capabilities"] == ["resource.write"]
    assert payload["resource_types"] == ["workspace"]
    assert payload["tools_by_capability"] == [
        {"intent": "CREATE", "capability": "resource.write", "tool": "workspace_write"}
    ]
    assert payload["pinned_reads"] == ["workspace_navigator"]
    assert payload["source"] == "resource"
    providers = {row["capability"]: row["providers"] for row in payload["providers_by_capability"]}
    assert providers["resource.write"] == ["workspace_provider", "git_provider"]


# ── 2. 截断保护（Phase 2 验收）──────────────────────────────


def test_acceptance_write_flood_does_not_drop_resource_read(window_on):
    """**验收**：候选池里塞满写工具，读取入口仍然留在最终窗口里。

    刻意用办公文档资源：``workspace_navigator`` 本来就是 CORE_TOOLS（会被无条件钉住），
    用它测不出新规则；``office_doc_read`` 不在核心清单里，只有资源层的读取保护能救它。

    保护的前提是读取入口**已经在候选池里**——它由窗口派生补进来（``resource_window``
    会带上同资源的读取入口），截断层负责不让它被 Top-K 挤掉。池里根本没有它时，
    那是"上游过滤"，属于 ``dropped_core`` 要报的故障，不该靠钉一个不存在的名字掩盖。
    """
    from app.agents.skills.mandatory_tools import apply_tool_window

    pool = [_cap("office_doc_edit"), _cap("office_doc_read")] + [
        _cap(f"noise_{i}") for i in range(8)
    ]
    final, snapshot = apply_tool_window(pool, limit=1, layer="test.resource_guard")
    names = [item.name for item in final]
    assert "office_doc_read" in names, "变更办公文档时读取入口必须被保住"
    assert "office_doc_read" in snapshot.pinned
    assert "resource.read" in snapshot.mandatory_reason


def test_guard_is_not_applied_when_flag_off(window_off):
    """关闭开关时**逐字不变**：同样的池子，读取入口照旧被截断掉。"""
    from app.agents.skills.mandatory_tools import apply_tool_window

    pool = [_cap("office_doc_edit"), _cap("office_doc_read")] + [
        _cap(f"noise_{i}") for i in range(8)
    ]
    final, snapshot = apply_tool_window(pool, limit=1, layer="test.resource_guard_off")
    names = [item.name for item in final]
    assert "office_doc_read" not in names
    assert snapshot.pinned == []
    assert "resource.read" not in snapshot.mandatory_reason


def test_read_guards_only_fires_for_mutating_pools():
    from app.agents.capabilities.policy.resource_window import read_guards

    assert read_guards([_cap("workspace_navigator"), _cap("workspace_read")]) == frozenset()
    assert read_guards([]) == frozenset()
    # 变更类工具在，但读取入口**不在池里** → 不钉（钉不存在的名字会变成 dropped_core 噪音）
    assert read_guards([_cap("office_doc_edit")]) == frozenset()
    guards = read_guards([_cap("office_doc_edit"), _cap("office_doc_read")])
    assert guards == frozenset({"office_doc_read"})
    workspace = read_guards([_cap("workspace_write"), _cap("workspace_navigator")])
    assert workspace == frozenset({"workspace_navigator"})


def test_guard_does_not_disturb_core_pinning(window_on):
    """保护规则是**叠加**的：核心工具仍然按原顺序占位，且不重复。"""
    from app.agents.skills.mandatory_tools import apply_tool_window

    pool = [_cap("workspace_navigator"), _cap("workspace_write")] + [
        _cap(f"noise_{i}") for i in range(5)
    ]
    final, snapshot = apply_tool_window(pool, limit=2, layer="test.core_plus_guard")
    names = [item.name for item in final]
    assert names[:2] == ["workspace_navigator", "workspace_write"], names
    assert len(names) == len(set(names)), "不允许重复注入"


# ── 3. 对拍口径 ─────────────────────────────────────────────


def test_resource_layer_diff_is_disclosed_not_parity(monkeypatch):
    """资源层差异**只增不减**，属披露维度：不能混进 switch_safe 判定。"""
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_RESOURCE_WINDOW_DIMENSION,
        shadow_compare,
        shadow_parity_totals,
    )

    diffs = shadow_compare()
    assert SHADOW_RESOURCE_WINDOW_DIMENSION in diffs, "资源层对拍维度必须存在"
    rows = diffs[SHADOW_RESOURCE_WINDOW_DIMENSION]
    for row in rows:
        intent, static, derived = row
        static_tools = {item for item in static.split(",") if item}
        derived_tools = {item for item in derived.split(",") if item}
        assert static_tools <= derived_tools, f"{intent} 的资源层窗口删了工具：{row}"
    totals = shadow_parity_totals(diffs)
    assert totals["switch_safe"] is True, diffs
    assert totals["declared_total"] >= len(rows)


def test_shadow_compare_windows_reports_only_differences():
    rows = rw.shadow_compare_windows({"READ": ("workspace_navigator",)})
    assert rows == []
    rows = rw.shadow_compare_windows({"CREATE": ("workspace_write",)})
    assert len(rows) == 1
    assert rows[0][0] == "CREATE"
    assert rows[0][1] == "workspace_write"
    assert "workspace_navigator" in rows[0][2]


def test_window_derivation_failure_falls_back_to_static(window_on, monkeypatch):
    """资源层抛异常时不能影响既有窗口（新层不该有拖垮主流程的权力）。"""
    import app.agents.capabilities.policy.resource_window as module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("resource layer exploded")

    monkeypatch.setattr(module, "resource_window", _boom, raising=True)
    # capability_preflight 是延迟 import 的，因此打补丁要打在模块属性上
    assert tool_window_for_actions(["MODIFY"]) == ACTION_TOOL_WINDOW["MODIFY"]
