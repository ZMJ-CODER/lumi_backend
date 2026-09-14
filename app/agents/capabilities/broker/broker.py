"""阶段 2：Capability Broker（能力的选择、授权、转发与结果归一）。

职责边界（方案第四节 4）：

1. 接收 Skill/执行节点提出的能力请求；
2. 按**会话绑定**（user/conversation/workspace/device/session）找 Provider；
3. 校验数据本地性与部署位置（``local_only`` 绝不落到服务端）；
4. 校验参数（能力自己的 ``input_schema``）与契约版本；
5. 处理超时、取消、幂等与流式游标；
6. 把结果折成统一 ``CapabilityResult``，并发布能力状态事件。

Broker **不做**：规划、模型路由、审批判定（审批由审批服务/策略层给结论）、直接执行
本地副作用——它只决定"这一步由谁做、允不允许、怎么把结果带回来"。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    DataLocality,
    Deployment,
    ProviderLease,
    RuntimeKind,
    SessionBinding,
    capability_failure,
    executor_type_for,
    parse_execution_plane,
    parse_runtime_kind,
)

from app.agents.capabilities.catalog.legacy import (
    CapabilityCatalog,
    capability_catalog,
)
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.registry.registry import (
    CapabilityProvider,
    CapabilityRegistry,
    ProviderRegistration,
    capability_registry,
)
from app.services.capability_events import (
    CAPABILITY_EVENT_FAILED,
    CAPABILITY_EVENT_REQUESTED,
    CAPABILITY_EVENT_STARTED,
    CAPABILITY_EVENT_WAITING_PROVIDER,
    events_for_result,
    publish_capability_event,
)
from app.services.capability_lease import CapabilityLeaseService

#: 未指定预算时的默认调用上界（秒）。与超时阶梯的 M1 档一致。
DEFAULT_DEADLINE_SECONDS = 10.0
#: 幂等结果缓存条数（进程内；跨 worker 需要 Redis，届时只换实现）。
IDEMPOTENCY_CACHE_SIZE = 256

#: Broker 只报**事实**的词汇表：底层预检原因（能力/租约/健康/权限/参数）。
#: 原因 → 对外冻结状态（``DEPENDENCY_MISSING`` 等）的唯一映射在
#: :data:`app.agents.orchestration.preflight.capability_preflight_service.LOW_LEVEL_TO_STATUS`；
#: Broker 不决定用户可见结论。
PREFLIGHT_FACTS: frozenset[str] = frozenset({
    "capability_unavailable",
    "provider_unhealthy",
    "permission_denied",
    "tool_not_registered",
    "workspace_missing",
    "approval_required",
    # 输入参数事实：不在对外映射表内（不构成预检阻断，由调用方按原错误码处理）。
    "invalid_arguments",
    # 能力已声明、但没有可用 Provider 的三种可执行原因（方案 4 §3.3：环境缺失只阻断，
    # 但**必须**给出正确的下一步——"没连设备"和"工作区不对"不是同一件事）。
    "provider_not_connected",
    "provider_binding_mismatch",
    "provider_unroutable",
})

#: 租约状态 → 预检事实（唯一映射；``""`` 与未知状态退回 ``provider_unhealthy``）。
_FACT_BY_LEASE_STATE: dict[str, str] = {
    "not_connected": "provider_not_connected",
    "binding_mismatch": "provider_binding_mismatch",
    "unroutable": "provider_unroutable",
    "unhealthy": "provider_unhealthy",
}

#: 租约状态 → 用户可执行的下一步（执行期失败文案；与预检的 next_action 同源口径）。
_SUGGESTED_ACTION_BY_LEASE_STATE: dict[str, str] = {
    "not_connected": "请启动/连接提供该能力的设备（Provider），完成能力注册后重试",
    "binding_mismatch": "该设备已连接但未绑定当前工作区，请切换到已授权的工作区后重试",
    "unroutable": "当前部署位置不允许该 Provider 执行，请改用允许的执行位置",
    "unhealthy": "该能力的租约已过期或心跳中断，请确认设备在线后重试",
}


def _preflight_v2_enabled() -> bool:
    """``CAPABILITY_PREFLIGHT_V2`` 是否打开（默认关闭）。"""
    from app.agents.orchestration.preflight.capability_preflight_service import FLAG
    from app.platform.runtime.feature_flags import feature_enabled

    return feature_enabled(FLAG)


class BrokerError(RuntimeError):
    """Broker 内部错误（不用于能力失败——失败一律走 ``CapabilityResult``）。"""


@dataclass(slots=True)
class CapabilitySelection:
    """一次选择的结论（可审计：为什么选了它/为什么没有）。"""

    descriptor: CapabilityDescriptor | None = None
    provider_id: str = ""
    deployment: Deployment | None = None
    lease: ProviderLease | None = None
    #: Broker 最终选中的 Provider **实际**执行位置与运行方式（取自租约）。
    execution_plane: str = ""
    runtime_kind: str = ""
    #: 位置切换是否由策略放行（hybrid 才可能出现 True）。
    routed_by_policy: bool = False
    reason: str = ""
    #: 没选到 Provider 时的**底层事实**（让"设备没连"与"连了但不属于这个工作区"
    #: 不再都显示成一句"没有可用的 Provider"）：
    #: ``not_connected`` / ``binding_mismatch`` / ``unhealthy`` / ``version_mismatch`` / ``""``。
    lease_state: str = ""

    @property
    def executor_type(self) -> str:
        """兼容旧字段（``server``/``client``/``worker``/``container``/``sandbox``）。"""
        if not self.execution_plane:
            return ""
        return executor_type_for(self.execution_plane, self.runtime_kind or RuntimeKind.IN_PROCESS)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.descriptor.qualified_name if self.descriptor else "",
            "provider_id": self.provider_id,
            "deployment": str(self.deployment or ""),
            "execution_plane": self.execution_plane,
            "runtime_kind": self.runtime_kind,
            "executor_type": self.executor_type,
            "lease_id": self.lease.lease_id if self.lease else "",
            "routed_by_policy": bool(self.routed_by_policy),
            "lease_state": self.lease_state,
            "reason": self.reason,
        }


#: 没选到 Provider 时的底层状态 → 失败原因（稳定文案，前端/预检据此给下一步）。
_NO_PROVIDER_REASON_BY_STATE: dict[str, str] = {
    "not_connected": "没有可用的 Provider（该能力尚无设备注册）",
    "binding_mismatch": "没有可用的 Provider（Provider 已连接但不属于当前工作区/会话）",
    "unhealthy": "没有可用的 Provider（已达成的租约已过期或心跳中断）",
    "unroutable": "没有可用的 Provider（当前部署位置不允许该 Provider 执行）",
}


def _no_provider_state(
    registered: list[ProviderLease],
    *,
    binding: Any,
    policy_allows_switch: bool,
    descriptor: CapabilityDescriptor,
) -> str:
    """为什么没选到 Provider（区分四种可执行的原因，供预检给出正确下一步）。

    优先级：从没注册 > 位置不允许 > 绑定不匹配 > 租约不可用（过期/心跳中断）。
    "绑定不匹配"要排在"租约不可用"之前：设备在线但工作区不对，用户需要绑对工作区，
    而不是去重连设备。
    """
    if not registered:
        return "not_connected"
    placement_ok = [
        lease for lease in registered
        if descriptor.allows_deployment(lease.deployment, policy_allows_switch=policy_allows_switch)
    ]
    if not placement_ok:
        return "unroutable"
    if binding is not None and not any(lease.matches(binding) for lease in placement_ok):
        return "binding_mismatch"
    return "unhealthy"


@dataclass(slots=True)
class _IdempotentEntry:
    result: CapabilityResult
    stored_at: float


@dataclass(frozen=True, slots=True)
class _PreflightProblem:
    """一次执行前失败的**底层事实**（Broker 不决定用户可见结论）。"""

    code: str
    message: str
    fact: str
    suggested_action: str = ""
    with_provider: bool = False
    details: dict[str, Any] = dataclasses.field(default_factory=dict)


class CapabilityBroker:
    """能力调用入口（服务端唯一转发点）。"""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        leases: CapabilityLeaseService | None = None,
        catalog: CapabilityCatalog | None = None,
        default_deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        policy_guard: Any = None,
    ) -> None:
        self._registry = registry or capability_registry
        self._leases = leases or CapabilityLeaseService(registry=self._registry)
        self._catalog = catalog or capability_catalog
        self._default_deadline = max(0.05, float(default_deadline_seconds))
        self._idempotent: dict[str, _IdempotentEntry] = {}
        self._policy_guard = policy_guard

    def _guard_for(self, policy_id: str = "") -> Any:
        """策略门禁：优先用注入的（测试/定制），否则用进程内共享门禁。"""
        if self._policy_guard is not None:
            return self._policy_guard
        from app.agents.capabilities.policy.policy_guard import policy_guard as shared

        return shared

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    @property
    def leases(self) -> CapabilityLeaseService:
        return self._leases

    # ── 租约可见性（唯一读取入口）─────────────────────────

    def _visible_leases(self) -> list[ProviderLease]:
        """**注册表可见**的活租约（Redis 权威缓存 ∪ 进程内副本）。

        为什么不能只读 ``self._leases.leases_for()``：那只看 ``CapabilityLeaseService``
        的进程内私有字典。注册端点（``/capabilities/register``）写进去的租约如果落在
        **另一个** ``CapabilityLeaseService`` 实例上，Broker 就永远看不到任何客户端
        Provider —— 于是提交期能力解析恒判"没有可用的 Provider"，把本机完全可用的
        ``workspace.read`` 报成缺能力（真实现象，已修）。

        ``CapabilityLeaseService.snapshot()`` 才是权威读入口：它合并 Redis 缓存（含
        ``register``/``heartbeat`` 时 seed 的条目与其它 worker 写入的租约）与进程内
        副本，并对过期租约做惰性清理。测试注入的轻量 stub 只实现 ``snapshot()``，
        这里统一走快照，不再依赖其它私有方法。
        """
        snapshot = getattr(self._leases, "snapshot", None)
        if not callable(snapshot):
            return []
        try:
            return list(snapshot())
        except Exception as exc:  # noqa: BLE001 - 租约视图不可用时按"无租约"处理
            logger.warning("[capability] 租约快照读取失败（按无租约处理）: {}", str(exc)[:160])
            return []

    def _provider_allowed_by_runtime_policy(self, provider_id: str) -> bool:
        """运行时策略是否允许这个 **Provider** 继续派发。

        逐个 Provider 判定（调用点在候选构造循环里）：只判断"是否所有 Provider 都被
        停用"会让部分停用失效。判定只查 ``provider:<id>`` 与 ``default`` 两层——
        能力（capability）粒度的覆盖不在这里，避免与能力预检的职责重叠。

        默认关闭时（``RUNTIME_POLICY_OVERRIDE=false``）是零开销直通。
        """
        try:
            from app.services.runtime_policy import policy_store, runtime_policy_enabled

            if not runtime_policy_enabled() or not provider_id:
                return True
            if not policy_store.enabled(provider_id=str(provider_id)):
                logger.debug("[capability] Provider 已被运行时策略停用 provider={}", str(provider_id)[:60])
                return False
        except Exception as exc:  # noqa: BLE001 - 策略读取失败不能把能力全停掉
            logger.debug("[capability] 运行时策略读取失败（按放行处理）: {}", str(exc)[:120])
        return True

    # ── 选择 ──────────────────────────────────────────────

    def _narrowing_provider_ids(
        self,
        capability: str,
        *,
        provider_ids: Iterable[str] | None = None,
        resource_type: str = "",
    ) -> frozenset[str]:
        """本次选择要用的 **Provider 收窄集合**（空集 = 不收窄）。

        两种给法，语义都是"资源类型允许哪些 Provider 承接"：

        * ``provider_ids`` 显式给定（调用方已经解析过）→ 原样使用；
        * 只给 ``resource_type`` → 经统一资源能力层推导
          （:func:`...broker.resource_dispatch.provider_ids_for`，只含**已注册**的候选）。

        **推导失败一律按"不收窄"处理**（返回空集）：这是安全边界上刻意的方向——
        收窄是"缩小候选"的优化，拿不到声明时应当回到收窄前的既有行为，
        而不是把能力判成不可用。与内核 :func:`lumi_capability.selection.select_lease`
        的"收窄不倒过来卡死"同一条原则。
        """
        if provider_ids:
            return frozenset(str(item) for item in provider_ids if str(item))
        wanted = str(resource_type or "").strip().casefold()
        if not wanted:
            return frozenset()
        try:
            from app.agents.capabilities.broker.resource_dispatch import provider_ids_for

            return frozenset(provider_ids_for(capability, wanted))
        except Exception as exc:  # noqa: BLE001 - 收窄推导失败按不收窄处理
            logger.debug("[capability] Provider 收窄集合推导失败: {}", str(exc)[:120])
            return frozenset()

    def select(
        self,
        capability: str,
        *,
        contract_version: int | None = None,
        binding: Any = None,
        preferred_deployment: str = "",
        policy_allows_switch: bool = False,
        provider_ids: Iterable[str] | None = None,
        resource_type: str = "",
    ) -> CapabilitySelection:
        """按绑定 + 本地性 + 健康状态选 Provider（不执行）。

        规则（顺序即优先级）：

        1. 目录里没声明该能力 → ``CAPABILITY_MISSING``（执行前失败，不拖到中途）；
        2. 契约版本不一致 → ``CONTRACT_VERSION_MISMATCH``；
        3. 只在**未过期且绑定匹配**的租约里选（客户端断线即无候选）；
        4. ``local_only`` 只接受客户端租约；``cloud`` 只接受服务端；
           ``hybrid`` 优先调用方期望的一侧，否则客户端优先（数据不出本机）；
        5. **资源类型收窄**（统一资源能力层，见 :meth:`_narrowing_provider_ids`）；
        6. 多个候选时取**最近心跳**的那个（多设备同能力时避免抖动）。

        ``provider_ids`` / ``resource_type`` 是**统一资源能力层**（方案《资源能力层》
        Phase 3 缺口 1）给 Broker 的收窄输入：**默认不传 = 逐字保持收窄前的行为**，
        因此"同一能力有多个 Provider"的既有选择结果不变；只有调用方明确说
        "这次是 workspace 资源的 resource.write"时，候选才会被收窄到该资源允许的
        Provider。收窄后一个候选都不剩时**保留收窄前的候选**并打 debug 日志——
        声明不完整（插件没声明资源类型）不该表现成"能力不可用"。
        """
        base = str(capability or "").split("@", 1)[0]
        descriptor = self._catalog.get(base)
        if descriptor is None:
            return CapabilitySelection(reason=f"未声明的能力：{base}")
        if contract_version is not None and int(contract_version) != descriptor.contract_version:
            return CapabilitySelection(
                descriptor=descriptor,
                reason=(
                    f"契约版本不一致：请求 {contract_version}，"
                    f"当前 {descriptor.contract_version}"
                ),
            )
        self._leases.sync_registry()
        candidates: list[ProviderLease] = []
        # 诊断用：该能力**注册过**的租约（不看健康/绑定/位置过滤），用于区分
        # "设备从没连过" vs "连过但绑定/位置/健康不匹配"。只影响失败原因，不影响选择。
        registered: list[ProviderLease] = []
        # 运行时策略停用的 Provider：**逐个过滤候选**，而不是"只要不是全部停用就放行"。
        # 后者是评审指出的 P0：三个 Provider 停掉两个，剩下那个照样被选中——运维以为
        # 停用生效了，实际流量还在往被停的设备上走。
        disabled: list[str] = []
        for lease in self._visible_leases():
            if lease.capability != base:
                continue
            if int(lease.contract_version) != int(descriptor.contract_version):
                continue
            registered.append(lease)
            if not self._provider_allowed_by_runtime_policy(lease.provider_id):
                disabled.append(str(lease.provider_id))
                continue
            if not lease.is_usable():
                continue
            if binding is not None and not lease.matches(binding):
                continue
            if not descriptor.allows_deployment(
                lease.deployment, policy_allows_switch=policy_allows_switch
            ):
                continue
            candidates.append(lease)
        if not candidates:
            if disabled and len(disabled) >= len(registered):
                # 所有已注册 Provider 都被运行时策略停用：如实报"提供方不可用"（沿用既有
                # 事实词，前端映射不变），并留下 disabled_providers 便于排障。
                return CapabilitySelection(
                    descriptor=descriptor,
                    reason=_NO_PROVIDER_REASON_BY_STATE["unhealthy"],
                    lease_state="unhealthy",
                )
            state = _no_provider_state(
                registered,
                binding=binding,
                policy_allows_switch=policy_allows_switch,
                descriptor=descriptor,
            )
            return CapabilitySelection(
                descriptor=descriptor,
                reason=_NO_PROVIDER_REASON_BY_STATE.get(state, "没有可用的 Provider"),
                lease_state=state,
            )
        # 资源类型收窄（统一资源能力层）。放在"没有候选"的早返回**之后**：
        # 收窄是用来"在多候选中挑对的那个"，而不是"把可用性判没"——收窄后为空时
        # 保留收窄前的候选（见函数说明与内核 select_lease 的同名规则）。
        wanted = self._narrowing_provider_ids(
            base, provider_ids=provider_ids, resource_type=resource_type
        )
        if wanted and candidates:
            narrowed = [item for item in candidates if str(item.provider_id) in wanted]
            if narrowed:
                candidates = narrowed
            else:
                logger.debug(
                    "[capability] 资源类型收窄后没有候选，按收窄前候选继续 "
                    "capability={} providers={}",
                    base,
                    sorted(wanted)[:4],
                )
        wanted_deployment = str(preferred_deployment or "").strip().casefold()
        if wanted_deployment:
            preferred = [item for item in candidates if str(item.deployment) == wanted_deployment]
            if preferred:
                candidates = preferred
        elif descriptor.data_locality is DataLocality.HYBRID:
            # hybrid 默认留在本地（数据不出本机）；需要上云必须由调用方/策略显式要求。
            local = [item for item in candidates if item.deployment is not Deployment.SERVER]
            if local:
                candidates = local
        head = max(candidates, key=lambda item: item.last_heartbeat_at)
        routed_by_policy = bool(
            descriptor.data_locality is DataLocality.HYBRID
            and head.deployment is Deployment.SERVER
            and policy_allows_switch
        )
        return CapabilitySelection(
            descriptor=descriptor,
            provider_id=head.provider_id,
            deployment=head.deployment,
            lease=head,
            # 选中即记录实际执行位置/运行方式（不是"目录里声明了什么"）。
            execution_plane=str(head.plane()),
            runtime_kind=str(head.runtime()),
            routed_by_policy=routed_by_policy,
            reason="ok",
        )

    # ── 调用 ──────────────────────────────────────────────

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
        preferred_deployment: str = "",
        policy_allows_switch: bool = False,
        job_id: str = "",
        policy_id: str = "",
        approval_context: dict[str, Any] | None = None,
        provider_ids: Iterable[str] | None = None,
        resource_type: str = "",
    ) -> CapabilityResult:
        """端到端一次能力调用（策略/审批 → 选择 → 参数校验 → 转发 → 事件 → 结果）。

        ``provider_ids`` / ``resource_type`` 与 :meth:`select` 同义，只在调用方
        已经知道"这次调用属于哪种资源"时传（默认不传 = 逐字保持既有选择行为）。
        """
        started = time.perf_counter()
        target_job = str(job_id or invocation.job_id or "")
        # 绑定以**服务端上下文的授权事实**为准；调用方没说自己是谁时才用它自述的值。
        # 注意 ``SessionBinding()`` 是"所有维度为空"的对象，本身为真值——不能用
        # ``or`` 兜底，否则空绑定会覆盖真实上下文（跨工作区匹配，越权读文件）。
        binding = _binding_or_context(invocation.session_binding, context.binding)
        # 策略包决定"能不能切服务端"：调用方传的 policy_allows_switch 不能**放宽**
        # 策略，只能被策略否决（hybrid 的云端切换必须同时满足两者）。
        guard = self._guard_for(policy_id)
        policy_allows_switch = bool(policy_allows_switch) and guard.cloud_switch_allowed(
            policy_id=policy_id
        )
        selection = self.select(
            invocation.capability,
            contract_version=invocation.contract_version,
            binding=binding,
            preferred_deployment=preferred_deployment,
            policy_allows_switch=policy_allows_switch,
            provider_ids=provider_ids,
            resource_type=resource_type,
        )
        await publish_capability_event(
            CAPABILITY_EVENT_REQUESTED,
            job_id=target_job,
            capability=invocation.qualified_capability,
            contract_version=invocation.contract_version,
            trace_id=invocation.trace_id,
            call_id=invocation.request_id,
            device_id=context.device_id,
            workspace_id=context.workspace_id,
            conversation_id=context.conversation_id,
            status="requested",
        )
        # 策略与审批：缺审批时返回 APPROVAL_REQUIRED（不是"直接执行"）。
        policy_failure: CapabilityResult | None = None
        verdict = None
        if selection.descriptor is not None:
            verdict, policy_failure = guard.authorize(
                selection.descriptor,
                invocation,
                policy_id=policy_id,
                approval_context=approval_context,
            )
        failure = policy_failure or self._preflight_failure(
            invocation, selection, context=context
        )
        if failure is not None:
            return await self._finish(
                failure, invocation=invocation, job_id=target_job, policy=verdict
            )

        # 幂等：同一 (能力, 幂等键, 绑定) 的重复请求直接返回上次结果。
        cached = self._idempotent_lookup(invocation, context)
        if cached is not None:
            return cached.model_copy(update={"request_id": invocation.request_id})

        assert selection.descriptor is not None  # preflight 已确认
        registration = self._registration_for(selection)
        if registration is None:
            result = capability_failure(
                CapabilityErrorCode.PROVIDER_OFFLINE,
                f"Provider {selection.provider_id} 未就绪（已断开或未注册）",
                capability=invocation.qualified_capability,
                provider_id=selection.provider_id,
                retryable=True,
            )
            return await self._finish(result, invocation=invocation, job_id=target_job)

        await publish_capability_event(
            CAPABILITY_EVENT_STARTED,
            job_id=target_job,
            capability=invocation.qualified_capability,
            provider_id=selection.provider_id,
            provider_version=registration.provider_version,
            contract_version=invocation.contract_version,
            device_id=selection.lease.device_id if selection.lease else "",
            trace_id=invocation.trace_id,
            call_id=invocation.request_id,
            status="running",
        )
        result = await self._call_provider(
            registration,
            invocation,
            context=context,
            selection=selection,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = result.model_copy(
            update={
                "capability": invocation.qualified_capability,
                "provider_id": selection.provider_id,
                "contract_version": invocation.contract_version,
                "request_id": invocation.request_id,
                "trace_id": invocation.trace_id,
                # 实际执行来源：以 Broker 选中的租约为准（Provider 自己没填就补上）。
                "execution_plane": result.execution_plane or (
                    parse_execution_plane(selection.execution_plane)
                    if selection.execution_plane
                    else None
                ),
                "runtime_kind": result.runtime_kind or (
                    parse_runtime_kind(selection.runtime_kind)
                    if selection.runtime_kind
                    else None
                ),
                "served_locally": bool(result.served_locally)
                or str(selection.execution_plane) == "client",
                "usage": result.usage.model_copy(
                    update={"duration_ms": elapsed_ms, "finished_at": time.time()}
                ),
            }
        )
        if result.ok:
            self._idempotent_store(invocation, context, result)
        return await self._finish(
            result, invocation=invocation, job_id=target_job, policy=verdict
        )

    # ── 内部：前置校验 ────────────────────────────────────

    def _preflight_problem(
        self,
        invocation: CapabilityInvocation,
        selection: CapabilitySelection,
        *,
        context: AgentExecutionContext | None = None,
    ) -> "_PreflightProblem | None":
        """执行前失败**事实**：绑定不符/缺能力/版本不符/参数非法。

        只回答"哪里不满足"，不回答"给用户看什么"：``fact`` 是 Broker 的底层
        原因词（:data:`PREFLIGHT_FACTS`），对外状态由 CapabilityPreflightService
        映射；``message``/``suggested_action`` 只用于开关关闭时的兼容路径。
        """
        if context is not None:
            mismatch = binding_mismatch(invocation, context)
            if mismatch:
                # 调用方不得用自己声明的绑定替换服务端注入的会话/工作区/设备。
                return _PreflightProblem(
                    code=CapabilityErrorCode.SCOPE_DENIED.value,
                    message="能力调用的会话绑定与服务端上下文不一致：" + "；".join(mismatch),
                    fact="permission_denied",
                    details={"mismatch": mismatch},
                )
        if selection.descriptor is None:
            return _PreflightProblem(
                code=CapabilityErrorCode.CAPABILITY_MISSING.value,
                message=f"未声明的能力 {invocation.qualified_capability}",
                fact="capability_unavailable",
                suggested_action="请安装或启用提供该能力的 Provider",
            )
        if not selection.provider_id:
            if selection.reason.startswith("契约版本不一致"):
                # 能力存在但版本不符：提示升级 Provider，而不是笼统的"缺能力"。
                return _PreflightProblem(
                    code=CapabilityErrorCode.CONTRACT_VERSION_MISMATCH.value,
                    message=f"{invocation.qualified_capability} 的契约版本不受支持（{selection.reason}）",
                    fact="capability_unavailable",
                    suggested_action="请升级或重新注册提供该能力的 Provider",
                    details={"reason": selection.reason},
                )
            return _PreflightProblem(
                code=CapabilityErrorCode.CAPABILITY_MISSING.value,
                message=f"没有可用 Provider 提供 {invocation.qualified_capability}",
                fact="provider_unhealthy",
                # 下一步必须与**真实原因**一致：设备没连 ≠ 工作区绑错 ≠ 位置不允许。
                # 错误码/fact 契约不变（前端分派不变），只有指引更准确。
                suggested_action=_SUGGESTED_ACTION_BY_LEASE_STATE.get(
                    str(getattr(selection, "lease_state", "") or ""),
                    "请确认客户端 Provider 已连接并完成能力注册",
                ),
                details={"reason": selection.reason, "lease_state": selection.lease_state},
            )
        problems = validate_arguments(selection.descriptor, invocation.arguments)
        if problems:
            return _PreflightProblem(
                code=CapabilityErrorCode.INVALID_ARGUMENTS.value,
                message="能力参数不合法：" + "；".join(problems[:4]),
                fact="invalid_arguments",
                with_provider=True,
                details={"problems": problems},
            )
        return None

    def preflight_facts(
        self,
        capabilities: Any,
        *,
        binding: Any = None,
    ) -> dict[str, str]:
        """**只报事实**的预检探测（``BrokerProbe`` 形状）。

        返回 ``{capability: 底层原因}``；没有问题的能力不出现在结果里。原因词取自
        :data:`PREFLIGHT_FACTS`，不含任何用户可见文案——判定与文案由
        CapabilityPreflightService 负责。
        """
        facts: dict[str, str] = {}
        for item in capabilities or ():
            capability = str(item or "").strip()
            if not capability:
                continue
            selection = self.select(capability, binding=binding)
            if selection.descriptor is None:
                facts[capability] = "capability_unavailable"
            elif not selection.provider_id:
                # 能力声明了但当下没有可用 Provider：按**底层原因**给事实，
                # 前端才能给出"连设备 / 绑工作区 / 等心跳恢复"的正确指引。
                facts[capability] = _FACT_BY_LEASE_STATE.get(
                    str(getattr(selection, "lease_state", "") or ""), "provider_unhealthy"
                )
        return facts

    def _preflight_failure(
        self,
        invocation: CapabilityInvocation,
        selection: CapabilitySelection,
        *,
        context: AgentExecutionContext | None = None,
    ) -> CapabilityResult | None:
        """执行前失败：绑定不符/缺能力/版本不符/参数非法（都不拖到中途才报）。

        开关关闭（默认）：返回历史结果（含用户可见文案），契约不变。
        开关打开：只返回底层事实（``fact`` + ``details.reason``），用户可见结论由
        CapabilityPreflightService / Orchestrator 给出。
        """
        problem = self._preflight_problem(invocation, selection, context=context)
        if problem is None:
            return None
        result = capability_failure(
            problem.code,
            problem.message,
            capability=invocation.qualified_capability,
            provider_id=selection.provider_id if problem.with_provider else "",
            suggested_action=problem.suggested_action,
            details=problem.details,
        )
        if not _preflight_v2_enabled():
            return result
        if result.error is None:  # pragma: no cover - capability_failure 恒带 error
            return result
        return result.model_copy(
            update={
                "error": result.error.model_copy(
                    update={
                        "message": problem.fact,
                        "suggested_action": "",
                        "details": {"reason": problem.fact},
                    }
                )
            }
        )

    def _registration_for(self, selection: CapabilitySelection) -> ProviderRegistration | None:
        for registration in self._registry.providers():
            if registration.provider_id == selection.provider_id:
                return registration
        return None

    # ── 内部：转发 ────────────────────────────────────────

    async def _call_provider(
        self,
        registration: ProviderRegistration,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
        selection: CapabilitySelection,
    ) -> CapabilityResult:
        import asyncio

        timeout = self._timeout_for(invocation)
        provider: CapabilityProvider = registration.provider
        try:
            return await asyncio.wait_for(
                provider.invoke(invocation, context=context),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return capability_failure(
                CapabilityErrorCode.TIMEOUT,
                f"能力 {invocation.qualified_capability} 在 {timeout:g}s 内未完成",
                capability=invocation.qualified_capability,
                provider_id=selection.provider_id,
                details={"timeout_seconds": timeout},
            )
        except asyncio.CancelledError:
            # 取消要向上传播（任务被终止/客户端断开），但先记录结构化状态。
            await publish_capability_event(
                "capability_failed",
                job_id=invocation.job_id,
                capability=invocation.qualified_capability,
                provider_id=selection.provider_id,
                status="failed",
                error_code=CapabilityErrorCode.CANCELLED.value,
                retryable=False,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - Provider 异常不能击穿 Broker
            logger.warning(
                "[capability] Provider {} 调用 {} 异常: {}",
                selection.provider_id, invocation.qualified_capability, str(exc)[:200],
            )
            return capability_failure(
                CapabilityErrorCode.FAILED,
                f"Provider 执行失败：{str(exc)[:200]}",
                capability=invocation.qualified_capability,
                provider_id=selection.provider_id,
            )

    def _timeout_for(self, invocation: CapabilityInvocation) -> float:
        """本次能力调用的超时（秒）。**所有时间轴都是 monotonic。**

        ``invocation.deadline`` 是 monotonic 绝对时刻（契约如此约定）；调用方没给时
        才退到"请求级剩余预算"——否则一个显式带 deadline 的调用会被请求预算意外截断，
        反过来也会出现"请求只剩 3 秒、能力却按 10 秒默认值跑完"的漏洞。

        另外把 ``self._default_deadline`` 作为上限：显式 deadline 比默认上限更宽松时
        取默认值（避免"忘了填"导致某次调用无限期挂住）。
        """
        if invocation.deadline and invocation.deadline > 0:
            remaining = float(invocation.deadline) - time.monotonic()
            if remaining <= 0:
                return 0.001
            return min(remaining, 3600.0)
        if invocation.timeout_seconds and invocation.timeout_seconds > 0:
            return min(float(invocation.timeout_seconds), 3600.0)
        # 没有显式预算：用请求级剩余预算收紧默认值（min，绝不放宽）。
        try:
            from app.platform.runtime.deadline import request_budget_seconds

            return max(0.001, request_budget_seconds(cap=self._default_deadline))
        except Exception:  # noqa: BLE001 - 预算不可用时用默认值
            return self._default_deadline

    # ── 内部：幂等 ────────────────────────────────────────

    def _idempotency_cache_key(
        self, invocation: CapabilityInvocation, context: AgentExecutionContext
    ) -> str:
        binding = invocation.session_binding or context.binding
        return "|".join(
            [
                invocation.qualified_capability,
                str(invocation.idempotency_key or ""),
                binding.key(),
            ]
        )

    def _idempotent_lookup(
        self, invocation: CapabilityInvocation, context: AgentExecutionContext
    ) -> CapabilityResult | None:
        if not invocation.idempotency_key:
            return None
        entry = self._idempotent.get(self._idempotency_cache_key(invocation, context))
        return entry.result if entry is not None else None

    def _idempotent_store(
        self,
        invocation: CapabilityInvocation,
        context: AgentExecutionContext,
        result: CapabilityResult,
    ) -> None:
        if not invocation.idempotency_key:
            return
        if len(self._idempotent) >= IDEMPOTENCY_CACHE_SIZE:
            oldest = min(self._idempotent, key=lambda key: self._idempotent[key].stored_at)
            self._idempotent.pop(oldest, None)
        self._idempotent[self._idempotency_cache_key(invocation, context)] = _IdempotentEntry(
            result=result, stored_at=time.time()
        )

    # ── 内部：收尾与事件 ──────────────────────────────────

    async def _finish(
        self,
        result: CapabilityResult,
        *,
        invocation: CapabilityInvocation,
        job_id: str,
        policy: Any = None,
    ) -> CapabilityResult:
        event = events_for_result(
            result,
            capability=invocation.qualified_capability,
            job_id=job_id,
            call_id=invocation.request_id,
        )
        # 策略结论随事件上链（前端"为什么被拒/为什么要审批"要有依据）。
        policy_fields: dict[str, Any] = {}
        if policy is not None and hasattr(policy, "to_snapshot"):
            snapshot = policy.to_snapshot()
            policy_fields = {
                "policy_id": snapshot.get("policy_id", ""),
                "policy_version": snapshot.get("policy_version", ""),
                "requires_approval": bool(snapshot.get("needs_approval")),
            }
        if not result.ok and result.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value:
            # 缺 Provider 时前端需要"等待 Provider"而不是"失败"。
            await publish_capability_event(
                CAPABILITY_EVENT_WAITING_PROVIDER,
                job_id=job_id,
                capability=invocation.qualified_capability,
                status="waiting_provider",
                error_code=result.error_code,
            )
        # 事件载荷 + 策略结论一起转发（前端"为什么被拒/为什么要审批"要有依据）。
        # 用"字典更新"而不是两处 kwargs：``requires_approval`` 等键在结果事件里已存在，
        # 两个 ``**`` 会直接 TypeError。
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"type", "job_id", "occurred_at"}
        }
        payload.update(policy_fields)
        await publish_capability_event(
            str(event.get("type") or CAPABILITY_EVENT_FAILED),
            job_id=job_id,
            **payload,
        )
        # 企业级审计：只有策略包要求时才落（``enterprise_audit``），只记指纹与时序，
        # 不含参数与正文。
        try:
            from app.agents.capabilities.audit.audit import record_if_audited

            binding = invocation.session_binding
            record_if_audited(
                invocation,
                result,
                policy_id=str(policy_fields.get("policy_id") or ""),
                device_id=str(getattr(binding, "device_id", "") or ""),
                workspace_id=str(getattr(binding, "workspace_id", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不能影响调用结果
            logger.debug("[capability] 审计落库失败（降级）: {}", str(exc)[:120])
        return result

    # ── 快照 ──────────────────────────────────────────────

    def capability_snapshot(self) -> list[dict[str, Any]]:
        """当前活跃能力绑定（进 Job.run_view 的 ``capability_snapshot``）。"""
        rows: list[dict[str, Any]] = []
        for lease in sorted(
            self._visible_leases(), key=lambda item: (item.capability, item.provider_id)
        ):
            descriptor = self._catalog.get(lease.capability, version=lease.contract_version)
            rows.append(
                {
                    "capability": lease.qualified_capability,
                    "provider_id": lease.provider_id,
                    "deployment": str(lease.deployment),
                    # 租约实际绑定的位置与运行方式（Job 快照据此回答"当时谁在执行"）。
                    "execution_plane": str(lease.plane()),
                    "runtime_kind": str(lease.runtime()),
                    "executor_type": lease.executor_type(),
                    "device_id": lease.device_id,
                    "workspace_id": lease.workspace_id,
                    "contract_version": int(lease.contract_version),
                    "data_locality": str(descriptor.data_locality) if descriptor else "",
                    "health_status": str(lease.health_status),
                }
            )
        return rows


def _binding_or_context(stated: SessionBinding, truth: SessionBinding) -> SessionBinding:
    """调用方自述绑定与服务端上下文合并（逐维度：自述优先，缺省用事实）。

    **不能**用 ``stated or truth``：``SessionBinding()`` 是"全空"对象但为真值，
    那样会让空绑定覆盖真实上下文，导致租约按"无绑定"匹配（跨工作区/跨设备命中）。
    """
    if not any(
        str(getattr(stated, field) or "")
        for field in ("user_id", "conversation_id", "workspace_id", "device_id", "session_id")
    ):
        return truth
    return SessionBinding(
        user_id=str(stated.user_id or truth.user_id or ""),
        conversation_id=str(stated.conversation_id or truth.conversation_id or ""),
        workspace_id=str(stated.workspace_id or truth.workspace_id or ""),
        device_id=str(stated.device_id or truth.device_id or ""),
        session_id=str(stated.session_id or truth.session_id or ""),
    )


def binding_mismatch(
    invocation: CapabilityInvocation,
    context: AgentExecutionContext,
) -> list[str]:
    """调用方自述绑定 vs 服务端上下文的差异（空=一致）。

    服务端注入的上下文是**授权事实**；``CapabilityInvocation.session_binding`` 可能是
    调用方（Skill/执行节点）构造的，因此只允许它"什么都不说"或与事实一致。
    不一致必须拒绝——否则调用方可以声称自己在另一个工作区，从而读别人的文件。
    """
    stated = invocation.session_binding
    truth = context.binding
    problems: list[str] = []
    for field in ("user_id", "conversation_id", "workspace_id", "device_id", "session_id"):
        claimed = str(getattr(stated, field) or "")
        actual = str(getattr(truth, field) or "")
        if claimed and actual and claimed != actual:
            problems.append(f"{field}：调用方声明 {claimed}，服务端上下文 {actual}")
        elif claimed and not actual:
            # 上下文没给这一维（例如无设备的服务端能力）而调用方声称有 → 以服务端为准。
            problems.append(f"{field}：调用方声明 {claimed}，服务端上下文没有该绑定")
    return problems


def validate_arguments(
    descriptor: CapabilityDescriptor,
    arguments: dict[str, Any],
) -> list[str]:
    """按能力自己的 ``input_schema`` 校验参数（返回问题列表，空=通过）。

    优先用 ``jsonschema``（已是运行依赖）；不可用时退化为类型/必填检查——**绝不**因为
    "校验库不在"就放过参数。
    """
    schema = descriptor.input_schema if isinstance(descriptor.input_schema, dict) else {}
    required = [str(item) for item in (schema.get("required") or [])]
    problems: list[str] = [f"缺少必填参数 {name}" for name in required if name not in arguments]
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for name, sub in properties.items():
        if name not in arguments or not isinstance(sub, dict):
            continue
        problems.extend(_type_problems(name, arguments[name], sub))
    try:
        import jsonschema  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - 没有校验库时用上面的最小检查
        return problems
    validator = jsonschema.Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.path)):
        pointer = ".".join(str(part) for part in error.path) or "(root)"
        message = f"{pointer}: {error.message}"
        if message not in problems:
            problems.append(message)
    return problems


def _type_problems(name: str, value: Any, schema: dict[str, Any]) -> list[str]:
    expected = schema.get("type")
    if not isinstance(expected, str):
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            return [f"{name} 只能是 {enum} 之一"]
        return []
    checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
        "null": lambda item: item is None,
    }
    check = checks.get(expected)
    if check is not None and not check(value):
        return [f"{name} 类型应为 {expected}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return [f"{name} 只能是 {enum} 之一"]
    return []


#: 进程内共享 Broker（阶段 2 的调用入口）。
#:
#: **必须复用注册端点用的那个共享租约服务**：``/capabilities/register``、
#: ``/heartbeat``、``/unregister`` 与 executor 的能力门禁都注入
#: ``app.services.capability_lease.capability_lease_service``。如果 Broker 自建一个
#: ``CapabilityLeaseService``，两者就是两套互不可见的进程内租约表 —— 客户端注册成功、
#: 注册表里也有 Provider，但 Broker 的 :meth:`CapabilityBroker.select` 永远空，
#: 于是 ``capability_resolution`` 把本机可用的 ``workspace.read@1`` 报成
#: "没有可用的 Provider"。``CapabilityLeaseService`` 内部是 (租约, ProviderID) 键的
#: 字典并按 ``owner_key`` 归一化主体，不存在"拿不到当前用户"的问题。
def _shared_lease_service() -> CapabilityLeaseService:
    from app.services.capability_lease import capability_lease_service

    return capability_lease_service


capability_broker = CapabilityBroker(leases=_shared_lease_service())


__all__ = [
    "BrokerError",
    "CapabilityBroker",
    "CapabilitySelection",
    "DEFAULT_DEADLINE_SECONDS",
    "IDEMPOTENCY_CACHE_SIZE",
    "binding_mismatch",
    "capability_broker",
    "validate_arguments",
]
