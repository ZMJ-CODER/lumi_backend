"""统一资源能力层 × Broker：**Provider 收窄**（两代对照缺口 1）回归。

缺口背景（`docs/CAPABILITY_TWO_GENERATIONS.md` §2.5 / §3）：`CapabilityBroker.select`
原先不认识 `resource_type` / `provider_ids`，于是"同一能力有多条租约"时 Broker 只能按
心跳选；而新层（`CapabilityDispatchAdapter` + `lumi_capability.selection.select_lease`）
会按资源类型收窄 —— 两条路径在真机上**必然选出不同 Provider**（实测：
`lumi.local.workspace` vs `lumi.local.code`），这正是"两代永不天然等价"的根因，
也是"新代是权威"这句话在 Broker 层站不住的原因。

本文件钉住四条主张：

1. **默认不传 = 逐字保持旧行为**（心跳最新者胜）——既有调用点零影响；
2. **显式 `provider_ids`** 能选中资源声明允许的 Provider；
3. **只给 `resource_type`** 时经统一资源能力层推导（与派发层同一份声明）；
4. **收窄不倒过来卡死**：收窄后一个候选都不剩时保留收窄前的候选——包括
   `memory_provider` 这种"声明了但实现还没注册"的已知不对齐（缺口 2），
   收窄失败绝不能表现成"能力不可用"。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityInvocation,
    CapabilityResult,
    Deployment,
    ProviderHealth,
    SessionBinding,
    capability_ok,
)

from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.broker.resource_dispatch import provider_ids_for
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.registry.builtin import (
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_WORKSPACE_READ,
)
from app.agents.capabilities.registry.registry import CapabilityRegistry
from app.services.capability_lease import CapabilityLeaseService

#: 两条租约都广告同一个能力（真机上就是"都在广告 code.execute"）：
#: 工作区 Provider 的**心跳更新**，代码 Provider 才是资源声明允许的那个。
_WORKSPACE_PROVIDER = "lumi.local.workspace"
_CODE_PROVIDER = "lumi.local.code"
_GIT_PROVIDER = "lumi.local.git"


class _RecordingProvider:
    """记录调用的假 Provider（只为证明"被选中的那个真的接到调用"）。"""

    def __init__(self, *, provider_id: str, descriptors: tuple[CapabilityDescriptor, ...]) -> None:
        self._provider_id = provider_id
        self._descriptors = descriptors
        self.calls: list[CapabilityInvocation] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self) -> Deployment:
        return Deployment.CLIENT

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def invoke(self, invocation, *, context) -> CapabilityResult:  # noqa: ANN001
        self.calls.append(invocation)
        return capability_ok(
            {"status": "ok"},
            capability=invocation.qualified_capability,
            provider_id=self._provider_id,
            served_locally=True,
        )


def _two_provider_stack(capability: str, *, providers: tuple[str, str]):
    """两条租约广告同一个能力；**第一个**的心跳更新（默认选择因此可预期）。

    返回 ``(broker, binding, provider_by_id)``。
    """
    from app.agents.capabilities.catalog.legacy import capability_catalog

    descriptor = capability_catalog.require(capability)
    registry = CapabilityRegistry()
    built = {
        name: _RecordingProvider(provider_id=name, descriptors=(descriptor,))
        for name in providers
    }
    leases = CapabilityLeaseService(
        registry=registry, provider_resolver=lambda lease: built[lease.provider_id]
    )

    async def setup() -> None:
        for name in reversed(providers):
            await leases.register(
                provider_id=name,
                capabilities=[{"capability": capability, "contract_version": 1}],
                user_id="u1",
                device_id="device-1",
                workspace_id="ws-1",
                ttl_seconds=60,
                health_status=ProviderHealth.HEALTHY.value,
            )
        # 心跳拉开**确定**的差距（不依赖注册顺序的时间精度）：第一条更新。
        for key, lease in list(leases._leases.items()):  # noqa: SLF001
            stamp = time.time() - (30 if key[1] != providers[0] else 0)
            leases._leases[key] = lease.model_copy(update={"last_heartbeat_at": stamp})  # noqa: SLF001
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(registry=registry, leases=leases)
    binding = SessionBinding(user_id="u1", device_id="device-1", workspace_id="ws-1")
    return broker, binding, built


@pytest.fixture()
def broker_with_two_providers():
    """`code.execute`：工作区 Provider 心跳更新，但资源声明只允许代码 Provider。"""
    return _two_provider_stack(
        CAPABILITY_CODE_EXECUTE, providers=(_WORKSPACE_PROVIDER, _CODE_PROVIDER)
    )


def test_without_narrowing_the_broker_keeps_picking_by_heartbeat(broker_with_two_providers):
    """**默认不传 = 旧行为**：多条租约时仍然只按心跳选（不引入任何新语义）。"""
    broker, binding, _providers = broker_with_two_providers

    selection = broker.select(CAPABILITY_CODE_EXECUTE, binding=binding)

    assert selection.provider_id == _WORKSPACE_PROVIDER
    assert selection.reason == "ok"


def test_explicit_provider_ids_narrow_the_candidates(broker_with_two_providers):
    """显式给出"这个资源允许哪些 Provider"→ 候选被收窄到其中，心跳不再是唯一判据。"""
    broker, binding, _providers = broker_with_two_providers

    selection = broker.select(
        CAPABILITY_CODE_EXECUTE,
        binding=binding,
        provider_ids=frozenset({_CODE_PROVIDER}),
    )

    assert selection.provider_id == _CODE_PROVIDER


def test_resource_type_is_resolved_through_the_resource_layer(broker_with_two_providers):
    """只给 `resource_type` 时用**统一资源能力层**的声明推导候选（与派发层同一份事实）。

    这条就是两代分歧的复现：旧路径（不收窄）选工作区，新路径（收窄到
    `code.execute` + `workspace` 允许的 Provider）选代码 Provider。
    """
    broker, binding, _providers = broker_with_two_providers
    expected = provider_ids_for(CAPABILITY_CODE_EXECUTE, "workspace")
    assert expected == frozenset({_CODE_PROVIDER}), "声明变了就要回来看这条用例"

    selection = broker.select(
        CAPABILITY_CODE_EXECUTE, binding=binding, resource_type="workspace"
    )

    assert selection.provider_id == _CODE_PROVIDER


def test_narrowing_that_excludes_every_candidate_falls_back(broker_with_two_providers):
    """收窄后一个候选都不剩 → **保留收窄前的候选**，绝不判成"没有可用 Provider"。"""
    broker, binding, _providers = broker_with_two_providers

    selection = broker.select(
        CAPABILITY_CODE_EXECUTE,
        binding=binding,
        provider_ids=frozenset({"lumi.local.not-installed"}),
    )

    assert selection.provider_id == _WORKSPACE_PROVIDER
    assert selection.lease_state == ""


def test_resource_type_whose_declaration_is_not_registered_does_not_break_selection(
    broker_with_two_providers,
):
    """已知不对齐（缺口 2a，已裁决为"只有声明"）的容忍：收窄集合为空 ⇒ 完全不收窄。

    `memory_provider` 声明了却没有实现，`provider_ids_for` 于是返回**空集**
    （它只收"已注册且有 provider_id"的候选）——空集在两边都被定义成"不收窄"，
    所以显式按 memory 收窄不会有任何副作用。即便将来收窄集合变成非空但与真租约不匹配，
    兜底规则也会保留收窄前候选。两种情况下结论都必须是"选得到 Provider"。
    """
    broker, binding, _providers = broker_with_two_providers
    assert provider_ids_for(CAPABILITY_CODE_EXECUTE, "memory") == frozenset()

    selection = broker.select(
        CAPABILITY_CODE_EXECUTE, binding=binding, resource_type="memory"
    )

    assert selection.provider_id == _WORKSPACE_PROVIDER


def test_invoke_passes_narrowing_through_to_selection():
    """`invoke` 与 `select` 必须同一条收窄口径（否则执行期又会选错 Provider）。

    这里用**只读**能力（`workspace.read` 不需要审批），让断言聚焦在"谁接到了调用"。
    """
    broker, binding, providers = _two_provider_stack(
        CAPABILITY_WORKSPACE_READ, providers=(_WORKSPACE_PROVIDER, _GIT_PROVIDER)
    )
    context = AgentExecutionContext.from_metadata(
        user_id="u1", conversation_id="c1", workspace_id="ws-1", device_id="device-1"
    )

    def _invocation(request_id: str) -> CapabilityInvocation:
        # ``idempotency_key`` 缺省等于 ``request_id``：两次调用必须换 id，
        # 否则第二次会命中幂等缓存（那是正确行为，但不是本用例要测的东西）。
        return CapabilityInvocation(
            capability=CAPABILITY_WORKSPACE_READ,
            arguments={"action": "read", "path": "a.py"},
            request_id=request_id,
            session_binding=binding,
        )

    # 不收窄：心跳更新的工作区 Provider。
    default_result = asyncio.run(broker.invoke(_invocation("req-1"), context=context))
    assert default_result.ok is True, default_result.error_code
    assert default_result.provider_id == _WORKSPACE_PROVIDER
    # 收窄到 git：即便心跳更旧，也必须是它接到调用。
    narrowed_result = asyncio.run(
        broker.invoke(
            _invocation("req-2"), context=context, provider_ids=frozenset({_GIT_PROVIDER})
        )
    )
    assert narrowed_result.ok is True, narrowed_result.error_code
    assert narrowed_result.provider_id == _GIT_PROVIDER
    assert providers[_GIT_PROVIDER].calls, "被收窄选中的 Provider 才应该收到调用"
    assert len(providers[_WORKSPACE_PROVIDER].calls) == 1, "默认那次只应调用一次"
