"""两代能力对照 · 缺口 3 **批次 3**：预检 / 审批 / 执行门禁的跨代守卫。

批次划分见 `docs/CAPABILITY_TWO_GENERATIONS.md` §3.5。这一批守的是**安全边界**：
前两批错了会"选得不理想"，这一批错了会"该拦的没拦"。所以断言分三类：

* **一致性**：预检事实必须与 Broker 的真实选择一一对应（不能一个说"没有 Provider"、
  另一个却能选出来）；预检 V2 开关只准改"谁写文案"，不准改错误码；
* **只紧不松**：审批档位上"自述只能收紧"，不认识的工具一律按最严处理，
  静态词表与派生档位对每个已知工具逐条相同；
* **门禁不因新层而放宽**：写/执行能力在无租约时**不允许**静默回退到旧路径；
  需要审批的能力在缺审批时**绝不**调用客户端。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityInvocation,
    Deployment,
    ProviderHealth,
    ProviderLease,
)

from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.broker.dispatch import (
    NEVER_FALLBACK_CAPABILITIES,
    CapabilityDispatchAdapter,
)
from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP, capability_catalog
from app.agents.capabilities.catalog.tool_registry import (
    LEGACY_TIER_TOOLS,
    risk_tier_of,
)
from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP
from app.agents.orchestration.preflight.capability_preflight import PreflightStatus
from app.agents.orchestration.preflight.capability_preflight_service import (
    FACT_NEXT_ACTIONS,
    LOW_LEVEL_TO_STATUS,
    CapabilityPreflightService,
)
from app.agents.capabilities.broker.broker import PREFLIGHT_FACTS
from app.agents.skills.approval_policy import classify_tool_risk, static_tier_of

#: 档位严度排序（`critical` 最严）。"自述只能收紧"= 派生值不得小于静态值。
_RANK = {"auto": 0, "routine": 1, "critical": 2}

_CATALOG_CAPABILITIES = [item.name for item in capability_catalog.all()]


# ── 夹具 ─────────────────────────────────────────────────────


def _lease(provider_id: str, capability: str, heartbeat: float = 100.0) -> ProviderLease:
    from app.services.capability_lease_redis import lease_id_for

    return ProviderLease(
        provider_id=provider_id,
        capability=capability,
        contract_version=1,
        lease_id=lease_id_for(provider_id, capability),
        user_id="u1",
        device_id="device-1",
        workspace_id="ws-1",
        conversation_id="c1",
        deployment=Deployment.CLIENT,
        plugin_id=provider_id,
        provider_version="1.0.0",
        health_status=ProviderHealth.HEALTHY.value,
        expires_at=9_999_999_999.0,
        last_heartbeat_at=heartbeat,
    )


class _LeaseService:
    def __init__(self, leases: list[ProviderLease] | None = None) -> None:
        self._leases = list(leases or [])

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)

    def sync_registry(self) -> None:
        return None

    async def refresh_from_redis(self) -> list[ProviderLease]:
        return list(self._leases)


class _RecordingCaller:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, name, tool_name, args=None, **kwargs):
        self.calls.append({"name": name, "tool": tool_name})
        return {"status": "ok", "content": "done"}


def _context():
    from app.agents.capabilities.contracts.context import AgentExecutionContext

    return AgentExecutionContext.from_metadata(
        user_id="u1", conversation_id="c1", workspace_id="ws-1", device_id="device-1"
    )


# ── 1. 一致性：预检事实 ⇔ Broker 的真实能力 ──────────────────


@pytest.mark.parametrize("with_leases", [False, True])
def test_preflight_facts_and_broker_selection_never_disagree(with_leases):
    """同一个绑定下，"预检说有" ⟺ "Broker 选得出 Provider"。

    两边只要有一处不一致，用户看到的就是"预检说没问题，执行时却说没有可用 Provider"
    （或反过来：预检说缺能力，其实完全可用）。这条对**整个能力目录**逐条对拍。
    """
    leases = [_lease("lumi.local.workspace", "workspace.read")] if with_leases else []
    broker = CapabilityBroker(leases=_LeaseService(leases))
    binding = _context().binding

    facts = broker.preflight_facts(_CATALOG_CAPABILITIES, binding=binding)
    for capability in _CATALOG_CAPABILITIES:
        selection = broker.select(capability, binding=binding)
        assert (capability in facts) == (selection.provider_id == ""), (
            capability,
            facts.get(capability),
            selection.provider_id,
            selection.reason,
        )


def test_preflight_facts_are_a_closed_vocabulary():
    """预检只报**底层事实**：词表闭集，且不得夹带用户可见文案。"""
    broker = CapabilityBroker(leases=_LeaseService())
    facts = broker.preflight_facts(_CATALOG_CAPABILITIES, binding=_context().binding)
    assert facts, "没有任何租约时，每个能力都应当有事实"
    assert set(facts.values()) <= set(PREFLIGHT_FACTS), facts
    # 对外状态映射只允许引用事实词（映射表自己不能凭空造词）
    assert set(LOW_LEVEL_TO_STATUS) <= set(PREFLIGHT_FACTS), set(LOW_LEVEL_TO_STATUS) - set(PREFLIGHT_FACTS)


@pytest.mark.parametrize("fact", sorted(LOW_LEVEL_TO_STATUS))
def test_any_probe_fact_blocks_the_preflight(fact):
    """探测报了事实 ⇒ 预检**绝不允许**给 READY（任何事实都不能被静默吞掉）。"""
    service = CapabilityPreflightService()
    result = service.preflight(
        profile={"required_capabilities": ["workspace.read@1"]},
        probe=lambda _caps, fact=fact: {"workspace.read@1": fact},
    )
    assert result.status != PreflightStatus.READY.value, (fact, result.status)
    assert result.status in {item.value for item in PreflightStatus}, result.status


@pytest.mark.parametrize(
    "fact",
    ["provider_unhealthy", "provider_not_connected", "provider_binding_mismatch", "provider_unroutable"],
)
def test_provider_facts_all_freeze_to_provider_unhealthy(fact):
    """四个 Provider 侧事实对外同属"提供方不可用"，但**下一步文案不同**。"""
    service = CapabilityPreflightService()
    result = service.preflight(
        profile={"required_capabilities": ["workspace.read@1"]},
        probe=lambda _caps, fact=fact: {"workspace.read@1": fact},
    )
    assert result.status == LOW_LEVEL_TO_STATUS[fact] == PreflightStatus.PROVIDER_UNHEALTHY.value
    expected_action = FACT_NEXT_ACTIONS.get(fact, "")
    if expected_action:
        assert result.override_next_action == expected_action


def test_every_frozen_status_is_reachable_through_its_own_channel():
    """冻结映射**只有一个来源**：每个事实由它真正的输入通道驱动，结论必须一致。"""
    service = CapabilityPreflightService()
    capability = "workspace.read@1"
    profile = {"required_capabilities": [capability]}

    cases = {
        "permission_denied": service.preflight(
            profile=profile,
            permissions={capability: False},
            required_permission="workspace.read",
        ),
        "approval_required": service.preflight(profile=profile, approval_required=True),
        "workspace_missing": service.preflight(
            profile=profile, workspace_bound=False, requires_workspace=True
        ),
        "tool_not_registered": service.preflight(
            profile=profile, registered_tools=("workspace_navigator",), desired_tools=("nope",)
        ),
    }
    for fact, result in cases.items():
        assert result.status == LOW_LEVEL_TO_STATUS[fact], (fact, result.status)


# ── 2. 只紧不松：审批档位 ────────────────────────────────────


@pytest.mark.parametrize("derived", [False, True])
def test_every_known_tool_has_the_same_tier_on_both_paths(monkeypatch, derived):
    """静态词表 vs 派生档位：认识的工具逐条相同；不派生的必须**明确登记**为遗留工具。

    "注册表不负责"如果没被登记，就会表现成"这个工具没有档位"——审批链路上那等于放行。
    注意豁免判据用的是**短名**（``code.edit`` 按 ``edit`` 豁免），所以这里也按短名核对。
    """
    from app.core.config import settings
    from app.agents.capabilities.catalog.tool_registry import _short_name

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", derived)
    for tool in sorted({*TOOL_CAPABILITY_MAP, *IMPLEMENTATION_MAP}):
        static_tier = static_tier_of(tool, {})[0]
        value = risk_tier_of(tool, {})
        if value is None:
            assert _short_name(tool).casefold() in LEGACY_TIER_TOOLS, (
                f"{tool} 没有被注册表接管，也没登记为遗留工具"
            )
            value = static_tier
        assert value == static_tier, (tool, value, static_tier)


def test_declaration_can_only_tighten_the_tier():
    """自述档位只能**收紧**：把 routine 的写工具说成 auto 不生效，说成 critical 生效。"""
    looser = SimpleNamespace(risk_tier="auto", capability="workspace.write", resource_type="workspace")
    tighter = SimpleNamespace(
        risk_tier="critical", capability="workspace.write", resource_type="workspace"
    )
    assert risk_tier_of("workspace_write", {}, looser) == "routine"
    assert risk_tier_of("workspace_write", {}, tighter) == "critical"


def test_unknown_tools_are_treated_as_critical_not_auto():
    """不认识的东西不能默认自动执行——这是审批链路上最后一道"保守"默认。"""
    assert risk_tier_of("totally_unknown_tool", {}) == "critical"
    assert risk_tier_of("demo_plugin_tool", {}) == "critical"
    # 静态词表同样保守（两条路径不能一个严一个松）
    assert classify_tool_risk("totally_unknown_tool", {})[0] == "critical"


def test_tier_derivation_never_loosens_the_static_baseline(monkeypatch):
    """对整个静态词表做一次"只紧不松"的全量检查（含参数级升级）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", True)
    arguments = {"path": "/", "recursive": True, "command": "git reset --hard"}
    for tool in sorted({*TOOL_CAPABILITY_MAP, *IMPLEMENTATION_MAP}):
        static_tier = static_tier_of(tool, arguments)[0]
        value = risk_tier_of(tool, arguments)
        if value is None:
            continue
        assert _RANK[value] >= _RANK[static_tier], (tool, value, static_tier)


# ── 3. 门禁不因新层而放宽 ────────────────────────────────────


def test_never_fallback_capabilities_are_all_local_only():
    """前提：不允许回退的能力必须是"本地数据"——否则它们根本不该走客户端派发。"""
    from lumi_contracts.plugins import DataLocality

    for capability in sorted(NEVER_FALLBACK_CAPABILITIES):
        descriptor = capability_catalog.get(capability)
        assert descriptor is not None, capability
        assert descriptor.data_locality is DataLocality.LOCAL_ONLY, capability


@pytest.mark.asyncio
async def test_write_and_execute_never_silently_fall_back_without_a_lease():
    """无租约时：写/执行能力**结构化失败**（handled=True），只有只读能力才允许回退。"""
    adapter = CapabilityDispatchAdapter(lease_service=_LeaseService(), call_tool=_RecordingCaller())
    context = _context()

    for capability in sorted(NEVER_FALLBACK_CAPABILITIES):
        outcome = await adapter.dispatch(
            capability=capability,
            args={"action": "read", "path": "a.py"},
            context=context,
            allow_legacy_fallback=True,  # 调用方"请求"回退也不行
        )
        assert outcome.handled is True, (capability, outcome.reason)
        assert outcome.result is not None and outcome.result.ok is False, capability

    read_outcome = await adapter.dispatch(
        capability="workspace.read",
        args={"action": "read", "path": "a.py"},
        context=context,
        allow_legacy_fallback=True,
    )
    assert read_outcome.handled is False, "只读能力允许回退到既有路径"


@pytest.mark.asyncio
async def test_capabilities_that_need_approval_never_reach_the_client():
    """缺审批 ⇒ 结构化 APPROVAL_REQUIRED，且客户端**一次都不能被调用**。"""
    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(
        lease_service=_LeaseService([_lease("lumi.local.code", "code.execute")]),
        call_tool=caller,
    )
    outcome = await adapter.dispatch(
        capability="code.execute",
        args={"language": "python", "code": "print(1)"},
        context=_context(),
        approval_context=None,
    )
    assert outcome.handled is True
    assert outcome.result is not None
    assert outcome.result.error_code == CapabilityErrorCode.APPROVAL_REQUIRED.value
    assert caller.calls == [], "审批未通过时不得调用客户端"


@pytest.mark.asyncio
async def test_preflight_v2_flag_changes_only_the_wording(monkeypatch):
    """V2 开关只决定"谁写文案"：错误码必须逐字不变（前端按错误码分派）。"""
    from app.core.config import settings

    broker = CapabilityBroker(leases=_LeaseService())
    context = _context()

    def _invocation(request_id: str) -> CapabilityInvocation:
        return CapabilityInvocation(
            capability="workspace.read",
            arguments={"action": "read", "path": "a.py"},
            request_id=request_id,
            session_binding=context.binding,
        )

    monkeypatch.setattr(settings, "CAPABILITY_PREFLIGHT_V2", False)
    legacy = await broker.invoke(_invocation("req-legacy"), context=context)
    monkeypatch.setattr(settings, "CAPABILITY_PREFLIGHT_V2", True)
    v2 = await broker.invoke(_invocation("req-v2"), context=context)

    assert legacy.ok is False and v2.ok is False
    assert legacy.error_code == v2.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value
    assert legacy.error is not None and v2.error is not None
    assert str(v2.error.message) in PREFLIGHT_FACTS, v2.error.message
    assert str(legacy.error.message) not in PREFLIGHT_FACTS, "关闭时给的是用户可见文案"
