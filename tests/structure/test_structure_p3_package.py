"""P3（抽 ``lumi_capability``）：包边界 + app 侧接线回归。

第一批只抽**纯决策**：词表、档位派生、部署判定、候选租约选择。
第二批再抽：审计记录结构、参数指纹、步骤级执行前门禁。
这一阶段要盯三件事：

1. **包是纯净的**：只依赖 ``lumi-contracts``；不 import app / 编排内核 /
   Redis / FastAPI / SQLAlchemy / 日志库（由门禁规则 1/5/7 阻塞，这里再断言一次）；
2. **app 侧调用面不变**：既有调用点写的是 ``catalog.resource.UNIFIED_*``、
   ``tool_registry.side_effect_tier``、``registry.descriptor_allows_deployment``、
   ``dispatch.select_lease``、``audit.audit.audit_record``、
   ``policy_guard.capability_fingerprint``、``gate.node_capability_failure``——
   抽包不许改它们；
3. **行为逐字不变**：分成"算法在包里、应用侧的表/目录/词表作为参数传进去"之后，
   应用侧结果必须与抽包前一致（``publish`` 档位、抽象能力映射是最典型的证据）。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib

import pytest

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT
PACKAGE_DIR = REPO_ROOT / "packages" / "capability"

CAP = "app.agents.capabilities"


def _gate():
    path = REPO_ROOT / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("_arch_gate_p3", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_arch_gate_p3"] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()


# ── 1. 包本身 ───────────────────────────────────────────────


def test_package_is_a_workspace_member_with_only_contract_dependency():
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    assert "packages/capability" in members
    assert root["tool"]["uv"]["sources"]["lumi-capability"]["workspace"] is True

    pkg = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert pkg["project"]["name"] == "lumi-capability"
    deps = [dep.split(">=")[0] for dep in pkg["project"]["dependencies"]]
    assert deps == ["lumi-contracts"], f"纯决策包只允许依赖契约层，实际：{deps}"


def test_package_modules_are_importable_and_documented():
    for name in ("vocabulary", "tiers", "deployment", "selection", "fingerprint", "audit", "gating"):
        module = importlib.import_module(f"lumi_capability.{name}")
        assert module.__doc__, f"lumi_capability.{name} 缺少模块 docstring"


def test_package_has_no_runtime_imports_today():
    """门禁规则 7 的当前树断言（阻塞规则，必须恒为零）。"""
    report = gate.check(rules={"7"})
    assert not report.new and not report.known, report.new


def test_rule_7_flags_a_runtime_import_in_a_pure_package():
    """对抗性用例：纯包里出现 redis / loguru 必须被拦下。"""
    found = gate.scan_source("import redis\n", module="lumi_capability.selection")
    assert {v.rule for v in found} == {"7"}
    assert any("redis" in v.detail for v in found)

    found = gate.scan_source("from loguru import logger\n", module="lumi_capability.tiers")
    assert "7" in {v.rule for v in found}
    assert any("loguru" in v.detail for v in found)

    found = gate.scan_source(
        "import importlib\nimportlib.import_module('app.core.redis')\n", module="lumi_capability.tiers"
    )
    assert "7" in {v.rule for v in found}, "字符串式动态导入同样是越界"
    assert any("动态导入" in v.detail for v in found)


def test_rule_7_does_not_touch_other_packages():
    """执行/编排内核不是"纯决策包"，规则 7 不管它们（规则 1/5 管）。"""
    found = gate.scan_source("import redis\n", module="lumi_execution.engine")
    assert "7" not in {v.rule for v in found}


# ── 2. app 侧接线：同一对象，不是复制 ───────────────────────


def test_vocabulary_is_reexported_by_identity():
    import lumi_capability as C

    resource = importlib.import_module(f"{CAP}.catalog.resource")
    assert resource.UNIFIED_CAPABILITIES is C.UNIFIED_CAPABILITIES
    assert resource.RESOURCE_TYPES is C.RESOURCE_TYPES
    assert resource.UNIFIED_CAPABILITY_ALIASES is C.UNIFIED_CAPABILITY_ALIASES
    assert resource.normalize_unified_capability is C.normalize_unified_capability
    assert resource.is_unified_capability is C.is_unified_capability


def test_app_tier_algorithm_delegates_to_package():
    """算法在包里、表在应用侧：这是"纯核可复用 + 应用可补充"的分工。"""
    import lumi_capability as C

    registry = importlib.import_module(f"{CAP}.catalog.tool_registry")
    from app.agents.capabilities.catalog.tool_tables import SIDE_EFFECT_TIER

    # 应用侧多一个合成副作用名 publish（来自 INTENT_SIDE_EFFECTS 的 SEND/PUBLISH 意图）
    assert SIDE_EFFECT_TIER["publish"] == "critical"
    assert registry.side_effect_tier(["publish"]) == "critical"
    assert C.side_effect_tier(["publish"]) == "routine", "契约词表不认识 publish，按未知处理"
    # 契约词表覆盖的取值两边必须逐字一致
    for effect, expected in C.SIDE_EFFECT_TIER.items():
        assert registry.side_effect_tier([effect]) == expected == C.side_effect_tier([effect])


@pytest.mark.parametrize(
    "fn,args",
    [
        ("_stricter", ("auto", "critical")),
        ("_normalize_tier", ("CRITICAL",)),
        ("_floor_for_local_confirmation", ("auto", True)),
    ],
)
def test_app_helpers_match_package_helpers(fn, args):
    import lumi_capability as C

    registry = importlib.import_module(f"{CAP}.catalog.tool_registry")
    pairs = {
        "_stricter": C.stricter,
        "_normalize_tier": C.normalize_tier,
        "_floor_for_local_confirmation": C.floor_for_local_confirmation,
    }
    assert getattr(registry, fn)(*args) == pairs[fn](*args)


def test_app_descriptor_allows_deployment_keeps_registration_signature():
    import lumi_capability as C

    registry = importlib.import_module(f"{CAP}.registry.registry")

    class _Descriptor:
        def __init__(self, allows: bool) -> None:
            self.allows = allows

        def allows_deployment(self, deployment):
            return self.allows

    class _Registration:
        def __init__(self, allows: list[bool]) -> None:
            self.descriptors = [_Descriptor(item) for item in allows]
            self.deployment = "server"

    assert registry.descriptor_allows_deployment(_Registration([True, True])) is True
    assert registry.descriptor_allows_deployment(_Registration([True, False])) is False
    assert C.descriptor_allows_deployment(_Registration([True]).descriptors, "server") is True


def test_app_select_lease_keeps_context_signature():
    """``dispatch.select_lease`` 仍然吃 ``AgentExecutionContext``（调用点不变）。"""
    from lumi_contracts.plugins import ProviderHealth

    dispatch = importlib.import_module(f"{CAP}.broker.dispatch")
    context = importlib.import_module(f"{CAP}.contracts.context")

    class _Lease:
        capability = "workspace.write"
        provider_id = "lumi.local.workspace"
        health_status = ProviderHealth.HEALTHY
        last_heartbeat_at = 1.0

        def matches(self, binding) -> bool:
            return True

        def is_expired(self) -> bool:
            return False

    ctx = context.AgentExecutionContext(binding=None)
    lease, reason = dispatch.select_lease([_Lease()], capability="workspace.write", context=ctx)
    assert reason == "ok" and lease is not None

    lease, reason = dispatch.select_lease([], capability="workspace.write", context=ctx)
    assert lease is None and "绑定" in reason


# ── 3. 第二批：审计结构、指纹、门禁 ─────────────────────────


def test_audit_module_reexports_package_objects_by_identity():
    import lumi_capability as C

    audit = importlib.import_module(f"{CAP}.audit.audit")
    assert audit.CapabilityAuditRecord is C.CapabilityAuditRecord
    assert audit.audit_record is C.audit_record
    assert audit.to_capability_result is C.to_capability_result
    assert audit.is_local_denial is C.is_local_denial
    assert audit.normalize_local_deny_reason is C.normalize_local_deny_reason
    assert audit.process_entry_for_result is C.process_entry_for_result
    assert audit.LOCAL_DENY_REASONS is C.LOCAL_DENY_REASONS
    # 运行时适配留在 app：进程内缓冲与"要不要审计"的开关
    assert hasattr(audit, "CapabilityAuditLog") and hasattr(audit, "audit_enabled")
    assert not hasattr(C, "CapabilityAuditLog"), "进程内缓冲不该进纯包"


def test_policy_guard_fingerprint_is_the_package_function():
    import lumi_capability as C

    guard = importlib.import_module(f"{CAP}.policy.policy_guard")
    assert guard.capability_fingerprint is C.capability_fingerprint
    assert guard.capability_fingerprint("workspace.write", {"path": "/a"}) == C.capability_fingerprint(
        "workspace.write", {"path": "/a"}
    )


def test_gate_injects_app_catalog_and_abstract_map():
    """门禁的纯核在包里；应用侧注入自己的目录与抽象词表，签名与行为不变。"""
    import lumi_capability as C

    gate = importlib.import_module(f"{CAP}.policy.gate")
    resolver = importlib.import_module(f"{CAP}.registry.resolver")
    assert gate.NodeCapabilityGate is C.NodeCapabilityGate
    assert gate.CAPABILITY_DEPENDENCY_MISSING == C.CAPABILITY_DEPENDENCY_MISSING

    # 抽象能力词表来自应用（resolver.ABSTRACT_CAPABILITY_MAP）
    abstract_key = sorted(resolver.ABSTRACT_CAPABILITY_MAP)[0]
    concrete, _ = gate.resolve_declared([abstract_key])
    assert concrete, f"{abstract_key} 应该能映射到具体能力"
    mapped = C.resolve_declared([abstract_key], abstract_map=resolver.ABSTRACT_CAPABILITY_MAP)[0]
    assert concrete == mapped

    class _Node:
        id = "n1"
        params = {"required_capabilities": ["totally.unknown.capability"]}

    assert gate.declared_capabilities(_Node()) == ["totally.unknown.capability"]
    failed = gate.node_capability_failure(_Node())
    assert failed is not None and failed.error_code == "CAPABILITY_MISSING"


def test_gate_default_catalog_is_the_app_singleton():
    """不传 catalog 时用应用的能力目录（这是"应用侧注入"的具体含义）。"""
    gate = importlib.import_module(f"{CAP}.policy.gate")
    legacy = importlib.import_module(f"{CAP}.catalog.legacy")

    class _Node:
        id = "n1"
        params = {"required_capabilities": [sorted(legacy.capability_catalog.names())[0]]}

    assert gate.evaluate_node_capabilities(_Node()).ok is True


# ── 4. 第三批：可见性状态机与窗口计划 ───────────────────────


def test_visibility_states_are_reexported_by_identity():
    import lumi_capability as C

    resource = importlib.import_module(f"{CAP}.catalog.resource")
    for name in ("STATE_UNREGISTERED", "STATE_REGISTERED", "STATE_VISIBLE", "STATE_AVAILABLE", "STATE_UNAVAILABLE"):
        assert getattr(resource, name) == getattr(C, name), name
    assert C.VISIBILITY_STATES == frozenset(
        {"unregistered", "registered", "visible", "available", "unavailable"}
    )


def test_resource_visibility_delegates_but_keeps_fact_gathering():
    """状态机在包里；app 侧只负责**取事实**（绑定是否存在、池状态多少）。"""
    import lumi_capability as C

    resource = importlib.import_module(f"{CAP}.catalog.resource")
    assert resource.resource_visibility("totally_unknown_tool") == C.STATE_UNREGISTERED
    assert resource.resource_visibility("workspace_write") == C.STATE_REGISTERED
    # 池状态探测失败（传一个探测不出来的对象）时回落 registered，绝不谎报可用
    assert resource.resource_visibility("workspace_write", capability=object()) in {
        C.STATE_REGISTERED,
        C.STATE_VISIBLE,
    }


def test_window_plan_class_is_the_package_class():
    """``ResourceWindowPlan`` 是包里那个类本身（同一对象），不是复制品。"""
    import lumi_capability as C

    window = importlib.import_module(f"{CAP}.policy.resource_window")
    assert window.ResourceWindowPlan is C.WindowPlan
    assert window.canonical_tool_for is not None


def test_window_planning_still_behaves_the_same():
    """窗口计划的三个不变量：只增不减、读入口提到最前、变更先读。"""
    window = importlib.import_module(f"{CAP}.policy.resource_window")

    plan = window.plan_for_actions(["CREATE"], fallback=("workspace_write",))
    assert "workspace_write" in plan.derived
    assert "workspace_navigator" in plan.pinned_reads, "变更类意图必须钉住同资源的读入口"
    names = window.plan_names(plan)
    assert names[0] == "workspace_navigator", "读入口排最前"
    assert set(names) >= {"workspace_navigator", "workspace_write"}
    # 旧窗口永远在（窗口只增不减）
    plan_with_fallback = window.plan_for_actions(["READ"], fallback=("legacy_tool",))
    assert window.plan_names(plan_with_fallback)[0] == "legacy_tool"


def test_read_guards_pin_only_pool_members():
    window = importlib.import_module(f"{CAP}.policy.resource_window")
    assert window.read_guards(["workspace_write", "workspace_navigator"]) == frozenset({"workspace_navigator"})
    # 读入口不在池里就不钉（避免 dropped_core 噪音）
    assert window.read_guards(["workspace_write"]) == frozenset()
    # 没有变更类工具就没有保护
    assert window.read_guards(["workspace_navigator"]) == frozenset()
