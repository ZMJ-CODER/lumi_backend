"""路由快照唯一投影器回归（权威规则）。

锁定四件事：
1. ``router_v2`` 是唯一权威版本，旧执行策略不能覆盖它；
2. 顶层 ``task_profile`` 只有一份权威形状（严格 M0-M3），旧画像只在 compat；
3. 两个开关都关时策略字段被清空（即使是复用的旧 routing）；
4. 顶层镜像与 ``route_decision`` 同源。
"""

from __future__ import annotations

from app.agents.orchestration.planning.route_snapshot import (
    MANAGED_KEYS,
    apply_route_snapshot,
    build_route_snapshot,
    legacy_task_profile_from_strict,
    public_policy_fields,
)

STRICT = {
    "complexity": "M2",
    "confidence": 0.9,
    "intent_type": "EXECUTE_ACTION",
    "side_effects": ["SEND"],
    "info_sources": ["WORKSPACE", "EXTERNAL_WEB"],
    "output_target": "WORKSPACE",
    "execution_target": "DESKTOP",
    "required_capabilities": ["DOCUMENT_READ"],
    "path_determinism": "UNKNOWN",
    "estimated_steps": 3,
    "risk_level": "REQUIRES_APPROVAL",
    "data_sensitivity": "PRIVATE",
    "context_size_estimate": "LARGE",
}
ROUTER_META = {
    "task_profile": dict(STRICT),
    "route_mode": "sequential_workflow",
    "route_reason_code": "MULTI_STEP",
    "safety_action": "REQUIRE_USER_APPROVAL",
    "assessor_source": "heuristic",
    "policy_version": "router_v2",
}
POLICY_META = {
    "task_profile": {
        "goal": "EXECUTE",
        "required_sources": ["WORKSPACE_READ"],
        "complexity": "SEQUENTIAL",
        "safety_level": "RISKY_WRITE",
        "has_side_effect": True,
        "needs_runtime_decision": True,
        "confidence": 0.8,
    },
    "execution_policy": "single_action_skill",
    "complexity": "SEQUENTIAL",
    "policy_version": "v2",
    "fallback_action": None,
}


def test_router_v2_is_authoritative_and_legacy_cannot_override():
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=True,
        router_meta=ROUTER_META,
        policy_meta=POLICY_META,
    )
    assert snapshot["policy_version"] == "router_v2"
    assert snapshot["task_profile"] == STRICT
    assert snapshot["route_mode"] == "sequential_workflow"
    # 旧策略的画像/复杂度没有覆盖权威值（旧 complexity=SEQUENTIAL 必须让位）。
    assert snapshot["complexity"] == "M2"
    assert snapshot["execution_policy"] == "sequential_workflow"
    assert snapshot["compat"]["execution_policy_version"] == "v2"
    assert snapshot["compat"]["execution_policy_v2"]["execution_policy"] == "single_action_skill"
    assert snapshot["compat"]["legacy_task_profile"]["complexity"] == "SEQUENTIAL"


def test_mirrors_are_same_source_as_route_decision():
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=False,
        router_meta=ROUTER_META,
    )
    decision = snapshot["route_decision"]
    assert decision["schema_name"] == "lumi.route_decision"
    # 方案 4 §2.3：schema v2（新增 intent/action_intents/target_*/preflight 位）。
    assert decision["schema_version"] == 2
    for mirror, key in (
        ("policy_version", "policy_version"),
        ("task_profile", "task_profile"),
        ("route_mode", "route_mode"),
        ("route_reason_code", "route_reason_code"),
        ("safety_action", "safety_action"),
    ):
        assert snapshot[mirror] == decision[key]
    # 只有 Router v2 时没有 compat 噪音
    assert "compat" not in snapshot
    # v2 顶层结构化字段来自画像，且未预检时**不写空对象**。
    assert decision["intent_type"] == STRICT["intent_type"]
    assert decision["action_intents"] == list(STRICT.get("action_intents") or [])
    assert decision["target_clarity"] == str(STRICT.get("target_clarity") or "")
    assert "capability_preflight" not in decision


def test_legacy_only_mode_never_occupies_authoritative_keys():
    snapshot = build_route_snapshot(
        router_v2_enabled=False,
        execution_policy_v2_enabled=True,
        policy_meta=POLICY_META,
    )
    assert "route_decision" not in snapshot
    assert "task_profile" not in snapshot
    assert "policy_version" not in snapshot
    assert snapshot["execution_policy"] == "single_action_skill"
    assert snapshot["complexity"] == "SEQUENTIAL"
    assert snapshot["compat"]["legacy_task_profile"]["goal"] == "EXECUTE"


def test_both_off_returns_empty_snapshot_and_apply_clears_stale_fields():
    routing = {
        "workspace_id": "ws-1",
        "steps": [{"id": "n1"}],
        "route_decision": {"schema_name": "lumi.route_decision"},
        "task_profile": {"complexity": "M1"},
        "policy_version": "router_v2",
        "execution_policy": "m1_atomic_read",
        "compat": {"execution_policy_version": "v2"},
    }
    snapshot = build_route_snapshot(router_v2_enabled=False, execution_policy_v2_enabled=False)
    assert snapshot == {}
    apply_route_snapshot(routing, snapshot)
    for key in MANAGED_KEYS:
        assert key not in routing, f"{key} 应被清理"
    # 与任务本身相关的字段保持不变
    assert routing["workspace_id"] == "ws-1"
    assert routing["steps"] == [{"id": "n1"}]


def test_existing_fallback_action_wins_over_policy_meta():
    snapshot = build_route_snapshot(
        router_v2_enabled=False,
        execution_policy_v2_enabled=True,
        policy_meta=POLICY_META,
        existing={"fallback_action": "workspace_m1_read"},
    )
    assert "fallback_action" not in snapshot


def test_blocked_route_does_not_write_empty_execution_policy():
    blocked = {
        "task_profile": {"complexity": "M1", "info_sources": ["WORKSPACE"]},
        "route_mode": "",
        "route_reason_code": "DEPENDENCY_MISSING_WORKSPACE",
        "safety_action": "ALLOW",
    }
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=False,
        router_meta=blocked,
    )
    assert snapshot["route_decision"]["route_mode"] == ""
    assert "execution_policy" not in snapshot
    # 兼容模式下旧策略仍然提供 execution_policy（旧执行器需要）
    compat_only = build_route_snapshot(
        router_v2_enabled=False,
        execution_policy_v2_enabled=True,
        policy_meta=POLICY_META,
    )
    assert compat_only["execution_policy"] == "single_action_skill"


def test_legacy_profile_derivation_rules():
    derived = legacy_task_profile_from_strict(STRICT)
    assert derived["goal"] == "EXECUTE"          # intent_type=EXECUTE_ACTION
    assert derived["has_side_effect"] is True     # bool(side_effects)
    assert derived["required_sources"] == ["WORKSPACE_READ", "PUBLIC_WEB"]
    assert derived["complexity"] == "SEQUENTIAL"  # M2
    assert derived["safety_level"] == "RISKY_WRITE"  # REQUIRES_APPROVAL
    assert derived["needs_runtime_decision"] is True  # path_determinism=UNKNOWN

    read_only = legacy_task_profile_from_strict(
        {"complexity": "M1", "info_sources": ["USER_PROVIDED"], "path_determinism": "KNOWN"}
    )
    assert read_only["goal"] == "GENERATE"
    assert read_only["has_side_effect"] is False
    assert read_only["complexity"] == "ATOMIC"
    assert read_only["safety_level"] == "READ_ONLY"
    assert read_only["needs_runtime_decision"] is False

    dynamic = legacy_task_profile_from_strict({"complexity": "M3"})
    assert dynamic["needs_runtime_decision"] is True  # M3 也算需要运行期决策


def test_public_policy_fields_never_expose_legacy_profile_at_top_level():
    snapshot = build_route_snapshot(
        router_v2_enabled=True,
        execution_policy_v2_enabled=True,
        router_meta=ROUTER_META,
        policy_meta=POLICY_META,
    )
    public = public_policy_fields(snapshot)
    assert public["policy_version"] == "router_v2"
    assert public["task_profile"] == STRICT
    assert public["route_decision"]["route_mode"] == "sequential_workflow"
    assert public["compat"]["legacy_task_profile"]["goal"] == "EXECUTE"
    assert not ({"goal", "required_sources"} & set(public["task_profile"]))
    assert public_policy_fields({}) == {}
