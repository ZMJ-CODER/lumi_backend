"""插件生命周期状态机 + 配额回归（方案 §4.2 / §4.3）。"""

from __future__ import annotations

import pytest

from lumi_contracts.plugins.lifecycle import (
    PluginQuota,
    QuotaAction,
    can_transition,
    enforce_quota,
    operation_policy,
    should_circuit_break,
    transition,
)


# ── 状态机 ───────────────────────────────────────────────


def test_legal_lifecycle_paths():
    assert transition("registered", "enabled") == "enabled"
    assert transition("enabled", "disabled") == "disabled"
    assert transition("disabled", "enabled") == "enabled"
    assert transition("enabled", "upgrading") == "upgrading"
    assert transition("upgrading", "enabled") == "enabled"
    assert transition("enabled", "uninstalling") == "uninstalling"
    assert transition("uninstalling", "uninstalled") == "uninstalled"
    assert transition("enabled", "crashed") == "crashed"
    assert transition("crashed", "disabled") == "disabled"
    assert transition("registered", "registered") == "registered"


def test_illegal_transitions_are_rejected():
    for source, target in [
        ("uninstalled", "enabled"),   # 终态不可复活
        ("registered", "crashed"),
        ("disabled", "upgrading"),    # 必须先启用再升级
        ("uninstalling", "enabled"),
    ]:
        assert not can_transition(source, target), f"{source}→{target} 不该合法"
        with pytest.raises(ValueError, match="非法插件状态迁移"):
            transition(source, target)


def test_circuit_breaker_after_repeated_crashes():
    assert not should_circuit_break(0)
    assert not should_circuit_break(2)
    assert should_circuit_break(3)
    assert should_circuit_break(5)
    assert should_circuit_break(2, threshold=2)
    assert not should_circuit_break("bad")


# ── 运行中 Job 的分级策略（§4.2 表）─────────────────────


def test_operation_policies_match_the_table():
    disable = operation_policy("disable")
    assert disable.inflight == "finish" and disable.new_steps == "preflight_block"
    assert not disable.requires_drain

    upgrade = operation_policy("UPGRADE")
    assert upgrade.requires_drain and upgrade.new_steps == "use_new_version"
    assert upgrade.inflight == "finish", "drain：旧 Worker 跑完当前 Job 才退出"

    uninstall = operation_policy("uninstall")
    assert uninstall.marks_uncertain and uninstall.inflight == "uncertain"
    assert uninstall.reason_code == "PLUGIN_UNINSTALLED"

    crash = operation_policy("crash")
    assert crash.inflight == "restart" and crash.new_steps == "normal"

    with pytest.raises(ValueError, match="未知插件操作"):
        operation_policy("teleport")


def test_policy_is_serialisable():
    payload = operation_policy("upgrade").as_dict()
    assert payload["requires_drain"] is True and payload["operation"] == "upgrade"


# ── 配额两级约束 ─────────────────────────────────────────


def test_within_quota_is_noop():
    decision = enforce_quota(PluginQuota(), output_bytes=100, elapsed_seconds=1.0)
    assert decision.action == QuotaAction.NONE.value and not decision.blocked


def test_output_over_limit_becomes_artifact_ref_not_failure():
    decision = enforce_quota(PluginQuota(max_output_bytes=1000), output_bytes=5000)
    assert decision.action == QuotaAction.ARTIFACT_REF.value
    assert decision.error_code == "", "输出超限不是错误，正文转引用即可"
    assert decision.exceeded == ("max_output_bytes",)
    assert not decision.blocked


def test_timeout_kills_worker_and_reports_resource_exceeded():
    decision = enforce_quota(PluginQuota(timeout_seconds=5), elapsed_seconds=9.0)
    assert decision.action == QuotaAction.KILL_WORKER.value
    assert decision.error_code == "PLUGIN_RESOURCE_EXCEEDED"
    assert decision.blocked


def test_builtin_plugins_get_soft_limits_only():
    decision = enforce_quota(PluginQuota(timeout_seconds=5), elapsed_seconds=9.0, hard_limits=False)
    assert decision.action == QuotaAction.WARN.value
    assert decision.error_code == "PLUGIN_RESOURCE_EXCEEDED"
    assert not decision.blocked, "内置插件软约束不杀进程"


def test_timeout_wins_over_output_and_error_code_is_registered():
    from lumi_contracts import spec_for

    decision = enforce_quota(
        PluginQuota(max_output_bytes=10, timeout_seconds=1), output_bytes=99, elapsed_seconds=2.0
    )
    assert decision.action == QuotaAction.KILL_WORKER.value
    assert set(decision.exceeded) == {"max_output_bytes", "timeout_seconds"}
    assert spec_for(decision.error_code).code == "PLUGIN_RESOURCE_EXCEEDED"
