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

import time
from dataclasses import dataclass
from typing import Any

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

from app.agents.capabilities.catalog import (
    CapabilityCatalog,
    capability_catalog,
)
from app.agents.capabilities.context import AgentExecutionContext
from app.agents.capabilities.registry import (
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
            "reason": self.reason,
        }


@dataclass(slots=True)
class _IdempotentEntry:
    result: CapabilityResult
    stored_at: float


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
        from app.agents.capabilities.policy_guard import policy_guard as shared

        return shared

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    @property
    def leases(self) -> CapabilityLeaseService:
        return self._leases

    # ── 选择 ──────────────────────────────────────────────

    def select(
        self,
        capability: str,
        *,
        contract_version: int | None = None,
        binding: Any = None,
        preferred_deployment: str = "",
        policy_allows_switch: bool = False,
    ) -> CapabilitySelection:
        """按绑定 + 本地性 + 健康状态选 Provider（不执行）。

        规则（顺序即优先级）：

        1. 目录里没声明该能力 → ``CAPABILITY_MISSING``（执行前失败，不拖到中途）；
        2. 契约版本不一致 → ``CONTRACT_VERSION_MISMATCH``；
        3. 只在**未过期且绑定匹配**的租约里选（客户端断线即无候选）；
        4. ``local_only`` 只接受客户端租约；``cloud`` 只接受服务端；
           ``hybrid`` 优先调用方期望的一侧，否则客户端优先（数据不出本机）；
        5. 多个候选时取**最近心跳**的那个（多设备同能力时避免抖动）。
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
        for lease in self._leases.leases_for(base, version=descriptor.contract_version):
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
            return CapabilitySelection(descriptor=descriptor, reason="没有可用的 Provider")
        wanted = str(preferred_deployment or "").strip().casefold()
        if wanted:
            preferred = [item for item in candidates if str(item.deployment) == wanted]
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
    ) -> CapabilityResult:
        """端到端一次能力调用（策略/审批 → 选择 → 参数校验 → 转发 → 事件 → 结果）。"""
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

    def _preflight_failure(
        self,
        invocation: CapabilityInvocation,
        selection: CapabilitySelection,
        *,
        context: AgentExecutionContext | None = None,
    ) -> CapabilityResult | None:
        """执行前失败：绑定不符/缺能力/版本不符/参数非法（都不拖到中途才报）。"""
        if context is not None:
            mismatch = binding_mismatch(invocation, context)
            if mismatch:
                # 调用方不得用自己声明的绑定替换服务端注入的会话/工作区/设备。
                return capability_failure(
                    CapabilityErrorCode.SCOPE_DENIED,
                    "能力调用的会话绑定与服务端上下文不一致：" + "；".join(mismatch),
                    capability=invocation.qualified_capability,
                    details={"mismatch": mismatch},
                )
        if selection.descriptor is None:
            return capability_failure(
                CapabilityErrorCode.CAPABILITY_MISSING,
                f"未声明的能力 {invocation.qualified_capability}",
                capability=invocation.qualified_capability,
                suggested_action="请安装或启用提供该能力的 Provider",
            )
        if not selection.provider_id:
            if selection.reason.startswith("契约版本不一致"):
                # 能力存在但版本不符：提示升级 Provider，而不是笼统的"缺能力"。
                return capability_failure(
                    CapabilityErrorCode.CONTRACT_VERSION_MISMATCH,
                    f"{invocation.qualified_capability} 的契约版本不受支持（{selection.reason}）",
                    capability=invocation.qualified_capability,
                    suggested_action="请升级或重新注册提供该能力的 Provider",
                    details={"reason": selection.reason},
                )
            return capability_failure(
                CapabilityErrorCode.CAPABILITY_MISSING,
                f"没有可用 Provider 提供 {invocation.qualified_capability}",
                capability=invocation.qualified_capability,
                suggested_action="请确认客户端 Provider 已连接并完成能力注册",
                details={"reason": selection.reason},
            )
        problems = validate_arguments(selection.descriptor, invocation.arguments)
        if problems:
            return capability_failure(
                CapabilityErrorCode.INVALID_ARGUMENTS,
                "能力参数不合法：" + "；".join(problems[:4]),
                capability=invocation.qualified_capability,
                provider_id=selection.provider_id,
                details={"problems": problems},
            )
        return None

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
        """调用上界：``deadline``（monotonic 绝对时间）优先，其次 ``timeout_seconds``。"""
        if invocation.deadline and invocation.deadline > 0:
            remaining = float(invocation.deadline) - time.monotonic()
            if remaining <= 0:
                return 0.001
            return min(remaining, 3600.0)
        if invocation.timeout_seconds and invocation.timeout_seconds > 0:
            return min(float(invocation.timeout_seconds), 3600.0)
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
            from app.agents.capabilities.audit import record_if_audited

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
            self._leases.snapshot(), key=lambda item: (item.capability, item.provider_id)
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
capability_broker = CapabilityBroker()


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
