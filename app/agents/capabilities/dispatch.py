"""能力派发适配层：``Broker.invoke`` → 客户端 MCP 原子工具（按租约）。

Broker 不实现 workspace/sandbox 逻辑；它只做三件事：

1. 能力名 → MCP 原子工具名（:data:`app.agents.capabilities.builtin.TOOL_CAPABILITY_MAP` 的反向映射）；
2. 按**租约 + 健康**选定 Provider（:meth:`CapabilityLeaseService.snapshot` 已含跨 worker 副本）；
3. 调 ``manager.call_tool(tool, args, route={provider_id, plugin_id, lease_id})``。

**为什么必须按租约派发**：MCP 原子工具直接调本机实现、不过健康门禁。如果只按"工具可达"
派发，租约被摘除后同名 ``workspace_read`` 仍能读本机文件——健康隔离与撤销通道就白做了。

失败语义（与客户端契约一致，不做 silent fallback）：

===========================  ==================================================
租约过期                      不回退到旧路径，返回 ``LEASE_EXPIRED``
Provider unhealthy            尝试同能力其它 Provider；都没有则 ``PROVIDER_UNHEALTHY``
明确撤销                      立即拒绝（``PROVIDER_OFFLINE``），不回退
只读能力、无租约               允许调用方**显式**回退旧路径（``allow_legacy_fallback``）
写/执行能力、无租约            结构化失败，**禁止**静默回退
===========================  ==================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityResult,
    DataLocality,
    ProviderHealth,
    ProviderLease,
    RuntimeKind,
    capability_failure,
    capability_ok,
    executor_type_for,
)

from app.agents.capabilities.builtin import capability_for_tool
from app.agents.capabilities.catalog import CapabilityCatalog, capability_catalog
from app.agents.capabilities.context import AgentExecutionContext

#: 能力 → 首选 MCP 原子工具名（客户端路由表里的规范入口）。
#:
#: ``code.scan`` 刻意**不在这里**：客户端没有等价的 MCP 工具（骨架由服务端聚合服务
#: 用客户端提供的原文解析，见 catalog 里的 code.scan 说明）。客户端只登记描述、
#: 不广告租约，因此派发时用调用方给的兜底工具名，且在无租约时按只读能力回退到
#: ``workspace_navigator(action=scan)``。
CAPABILITY_TOOL_MAP: dict[str, str] = {
    "workspace.read": "workspace_navigator",
    "workspace.write": "workspace_write",
    # 操作契约族：编辑/移动/删除没有独立的客户端原子工具，由服务端操作网关
    # （app/services/workspace_operations.py）在客户端原子工具之上编排；这里登记
    # 规范入口名只为审批/审计/dispatch-map 有稳定的工具↔能力对照。
    "workspace.edit": "workspace_edit",
    "workspace.move": "workspace_move",
    "workspace.delete": "workspace_delete",
    "code.execute": "sandbox_run",
    # ``workspace_diff`` 是 git.operations 的规范入口（真的会读工作区做 diff）；
    # ``git`` 是**实现名**（见 catalog.IMPLEMENTATION_MAP 的注释），不是客户端原子工具名。
    # 这里曾经登记 ``git``，于是反查"能力→规范入口"会得到 ``git`` 而静态表别处写
    # ``workspace_diff`` —— 统一注册表的影子对比把这条不一致暴露出来（本轮修正）。
    "git.operations": "workspace_diff",
    "artifact.create": "create_office_document",
}

#: 写/执行类能力：**不允许**静默回退到旧执行路径（避免绕过租约/授权模型）。
#:
#: 四个工作区操作能力都在这里：它们的失败面包含"半截副作用"，任何"回退到直接调
#: 客户端原子工具"的做法都会绕过版本校验与回收站语义。
NEVER_FALLBACK_CAPABILITIES: frozenset[str] = frozenset(
    {
        "workspace.write",
        "workspace.edit",
        "workspace.move",
        "workspace.delete",
        "code.execute",
        "git.operations",
    }
)


@dataclass(slots=True)
class DispatchOutcome:
    """一次派发尝试的结果。``handled=False`` 表示调用方应走旧路径。"""

    handled: bool
    result: CapabilityResult | None = None
    provider_id: str = ""
    lease_id: str = ""
    mcp_tool: str = ""
    capability: str = ""
    reason: str = ""
    route: dict[str, Any] = field(default_factory=dict)
    #: 本次派发实际执行的来源（位置 + 运行方式），取自选中的租约。
    execution_plane: str = ""
    runtime_kind: str = ""

    @property
    def executor_type(self) -> str:
        if not self.execution_plane:
            return ""
        return executor_type_for(self.execution_plane, self.runtime_kind or RuntimeKind.IN_PROCESS)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "capability": self.capability,
            "provider_id": self.provider_id,
            "lease_id": self.lease_id,
            "mcp_tool": self.mcp_tool,
            "reason": self.reason,
            "execution_plane": self.execution_plane,
            "runtime_kind": self.runtime_kind,
            "executor_type": self.executor_type,
            "ok": bool(self.result and self.result.ok),
            "error_code": self.result.error_code if self.result else "",
        }


def mcp_tool_for_capability(capability: str, *, fallback: str = "") -> str:
    """能力 → 首选 MCP 原子工具。

    **真相源**：``TOOL_REGISTRY_DERIVED`` 打开时先问统一注册表；关闭时（默认）逐字走
    静态 ``CAPABILITY_TOOL_MAP``。两条路径的结果由 ``tool_registry.shadow_compare()``
    持续对拍（当前三个维度均无差异）。
    """
    base = str(capability or "").split("@", 1)[0]
    try:
        from app.agents.capabilities.tool_registry import registry_derived_enabled, resolve_tool

        if registry_derived_enabled():
            # 用"能力 → 首选工具"的反查：注册表里同能力可能有多个工具，取规范入口。
            for entry in _registry_entries():
                if entry.capability == base and entry.mcp_target:
                    return entry.mcp_target
            entry = resolve_tool(base)
            if entry is not None and entry.mcp_target:
                return entry.mcp_target
    except Exception as exc:  # noqa: BLE001 - 注册表不可用时静默回到静态表
        logger.debug("[capability] MCP 目标派生失败（回到静态表）: {}", str(exc)[:120])
    return CAPABILITY_TOOL_MAP.get(base, fallback)


def _registry_entries() -> list[Any]:
    from app.agents.capabilities.tool_registry import build_registry_entries

    return build_registry_entries()


def adapt_to_mcp_tool(tool_name: str, args: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """工具名 + 参数 → (MCP 原子工具名, MCP 参数)。

    目前是**恒等映射**（工具名已经是 MCP 原子名），保留这一层的意义是：
    * 参数需要按 MCP 侧约定整形时只改这里（不散落到 Broker/executor）；
    * 便于把 ``workspace_navigator`` 这类聚合入口与 ``workspace_read`` 原子入口统一。
    """
    return str(tool_name or ""), dict(args or {})


def _base(capability: str) -> str:
    return str(capability or "").split("@", 1)[0]


def select_lease(
    leases: list[ProviderLease],
    *,
    capability: str,
    context: AgentExecutionContext,
    require_healthy: bool = True,
    provider_ids: frozenset[str] | None = None,
) -> tuple[ProviderLease | None, str]:
    """在候选租约里选一个（返回 ``(租约, 未命中原因)``）。

    顺序：能力匹配 → 绑定匹配（用户/会话/工作区/设备）→ **资源类型收窄** →
    未过期 → 健康 → 最近心跳。

    ``provider_ids`` 来自统一资源能力层（"这个资源类型允许哪些 Provider 承接"）。
    收窄**只在不倒过来卡死的前提下生效**：收窄后一个候选都不剩时保留原候选——
    声明不完整（插件没声明资源类型）不该表现成"工具不可用"。
    """
    base = _base(capability)
    bound = [
        lease
        for lease in leases
        if lease.capability == base and lease.matches(context.binding)
    ]
    if provider_ids and bound:
        narrowed = [lease for lease in bound if str(lease.provider_id) in provider_ids]
        if narrowed:
            bound = narrowed
        else:
            logger.debug(
                "[capability] 资源类型收窄后没有候选，按收窄前候选继续 capability={} providers={}",
                base,
                sorted(provider_ids)[:4],
            )
    if not bound:
        return None, "没有与该会话绑定匹配的租约"
    alive = [lease for lease in bound if not lease.is_expired()]
    if not alive:
        # 过期租约**不**回退：客户端契约里过期就是"必须重新注册"。
        return None, "租约已过期（需要客户端重新注册能力）"
    if require_healthy:
        healthy = [
            lease
            for lease in alive
            if lease.health_status in {ProviderHealth.HEALTHY, ProviderHealth.UNKNOWN}
        ]
        if healthy:
            alive = healthy
        else:
            return None, "Provider 健康检查未通过（隔离中）"
    head = max(alive, key=lambda lease: lease.last_heartbeat_at)
    return head, "ok"


class CapabilityDispatchAdapter:
    """按租约把能力调用派发到客户端 MCP 原子工具。"""

    def __init__(
        self,
        *,
        lease_service: Any,
        catalog: CapabilityCatalog | None = None,
        call_tool: Any = None,
    ) -> None:
        self._leases = lease_service
        self._catalog = catalog or capability_catalog
        self._call_tool = call_tool

    def _caller(self) -> Any:
        if self._call_tool is not None:
            return self._call_tool
        from app.agents.mcp import manager

        return manager.call_tool

    def _authorize(
        self,
        descriptor: Any,
        *,
        capability: str,
        args: dict[str, Any] | None,
        approval_context: dict[str, Any] | None,
    ) -> Any | None:
        """审批门禁：需要审批且审批无效 → 返回结构化失败（否则 None）。

        复用 Broker 的 ``PluginPolicyGuard``（同一套 Policy Pack 与指纹校验），
        因此"哪些能力要审批"只有一个判定处。

        注意：**没有审批上下文也算未审批**——需要审批的能力不允许"不带上下文就直接放行"，
        否则整条审批链路可以被绕过。
        """
        from lumi_contracts.plugins import CapabilityInvocation

        try:
            from app.agents.capabilities.policy_guard import policy_guard

            context = dict(approval_context or {})
            invocation = CapabilityInvocation(
                capability=capability,
                arguments=dict(args or {}),
                request_id=str(context.get("request_id") or ""),
            )
            _verdict, failure = policy_guard.authorize(
                descriptor,
                invocation,
                policy_id=str(context.get("policy_id") or ""),
                approval_context=context or None,
            )
        except Exception as exc:  # noqa: BLE001 - 门禁异常按"未审批"处理（保守）
            logger.warning("[capability] 审批门禁异常（按未审批处理）: {}", str(exc)[:160])
            return capability_failure(
                CapabilityErrorCode.APPROVAL_INVALID,
                "能力审批校验失败，已阻止本次调用",
                capability=descriptor.qualified_name,
            )
        if failure is not None and not failure.ok:
            return failure
        return None

    async def dispatch(
        self,
        *,
        capability: str,
        args: dict[str, Any] | None = None,
        context: AgentExecutionContext,
        tool_name: str = "",
        allow_legacy_fallback: bool = False,
        timeout_s: float | None = None,
        task_id: str | None = None,
        call_id: str | None = None,
        approval_context: dict[str, Any] | None = None,
        provider_ids: frozenset[str] | None = None,
    ) -> DispatchOutcome:
        """尝试按租约派发；``handled=False`` 时调用方走旧路径。

        ``approval_context`` 给定时先过**能力审批门禁**（与 Broker 同一套策略与指纹
        校验）：缺审批/指纹不符直接 ``APPROVAL_REQUIRED``，**不会**去调客户端——避免
        "服务端未审批"与"客户端本地拒止"两条链路各说各话。

        ``provider_ids`` 是统一资源能力层给出的"该资源类型允许的 Provider"，
        只用于收窄候选（Phase 3）；为空表示不收窄。
        """
        base = _base(capability)
        descriptor = self._catalog.get(base)
        if descriptor is None:
            return DispatchOutcome(
                handled=False, capability=base, reason=f"未声明的能力：{base}"
            )
        if descriptor.data_locality is not DataLocality.LOCAL_ONLY:
            # cloud 能力由服务端自己执行，不经客户端 MCP。
            return DispatchOutcome(
                handled=False, capability=base, reason="cloud 能力不由客户端派发"
            )
        # 审批门禁（在选 Provider 之前）：需要审批的能力没有有效审批 → 结构化失败。
        denial = self._authorize(
            descriptor, capability=base, args=args, approval_context=approval_context
        )
        if denial is not None:
            return DispatchOutcome(
                handled=True,
                capability=base,
                reason=str(denial.error_code or "approval_required"),
                result=denial,
            )
        # 跨 worker：先同步一次权威租约，避免"worker B 查不到 A 注册的租约"。
        refresh = getattr(self._leases, "refresh_from_redis", None)
        if callable(refresh):
            try:
                await refresh()
            except Exception as exc:  # noqa: BLE001 - 同步失败沿用本地副本
                logger.debug("[capability] 租约同步失败（沿用本地副本）: {}", str(exc)[:120])
        lease, reason = select_lease(
            self._leases.snapshot(),
            capability=base,
            context=context,
            provider_ids=provider_ids,
        )
        if lease is None:
            # 只读能力可以按调用方显式授权回退旧路径；写/执行一律结构化失败。
            if allow_legacy_fallback and base not in NEVER_FALLBACK_CAPABILITIES:
                return DispatchOutcome(handled=False, capability=base, reason=reason)
            code = (
                CapabilityErrorCode.LEASE_EXPIRED
                if "过期" in reason
                else (
                    CapabilityErrorCode.PROVIDER_UNHEALTHY
                    if "健康" in reason
                    else CapabilityErrorCode.CAPABILITY_MISSING
                )
            )
            return DispatchOutcome(
                handled=True,
                capability=base,
                reason=reason,
                result=capability_failure(
                    code,
                    f"{base} 无法派发：{reason}",
                    capability=descriptor.qualified_name,
                    retryable=code
                    in {
                        CapabilityErrorCode.LEASE_EXPIRED,
                        CapabilityErrorCode.PROVIDER_UNHEALTHY,
                    },
                    suggested_action="请确认客户端 Provider 已连接并在「插件与能力」面板中未被隔离",
                ),
            )
        mcp_tool, mcp_args = adapt_to_mcp_tool(
            tool_name or mcp_tool_for_capability(base), args
        )
        route = {
            "provider_id": lease.provider_id,
            "plugin_id": lease.plugin_id,
            "lease_id": lease.lease_id,
            "capability": lease.qualified_capability,
            # 实际执行来源随路由一起下发：客户端日志/审计能对上"谁在执行"。
            "execution_plane": str(lease.plane()),
            "runtime_kind": str(lease.runtime()),
            "executor_type": lease.executor_type(),
        }
        caller = self._caller()
        try:
            payload = await caller(
                lease.provider_id,
                mcp_tool,
                mcp_args,
                task_id=task_id,
                call_id=call_id,
                timeout_s=timeout_s,
                user_id=context.user_id,
                device_id=context.device_id,
                workspace_id=context.workspace_id,
                conversation_id=context.conversation_id,
                route=route,
            )
        except TypeError:
            # 测试替身/旧签名不接受 route 时退化为不带路由（仍按租约选中了 Provider）。
            payload = await caller(lease.provider_id, mcp_tool, mcp_args)
        if payload is None:
            return DispatchOutcome(
                handled=True,
                capability=base,
                provider_id=lease.provider_id,
                lease_id=lease.lease_id,
                mcp_tool=mcp_tool,
                route=route,
                reason="客户端调用失败或熔断",
                result=capability_failure(
                    CapabilityErrorCode.PROVIDER_OFFLINE,
                    f"{base} 的客户端调用失败（连接中断或熔断）",
                    capability=descriptor.qualified_name,
                    provider_id=lease.provider_id,
                    retryable=True,
                ),
            )
        status = str((payload or {}).get("status") or "ok")
        ok = status in {"ok", "success", "partial", "empty"}
        # 客户端派发：实际执行在用户设备上（位置来自租约，运行方式由租约推导/声明）。
        plane = str(lease.plane())
        runtime = str(lease.runtime())
        return DispatchOutcome(
            handled=True,
            capability=base,
            provider_id=lease.provider_id,
            lease_id=lease.lease_id,
            mcp_tool=mcp_tool,
            route=route,
            reason="ok" if ok else status,
            execution_plane=plane,
            runtime_kind=runtime,
            result=(
                capability_ok(
                    payload,
                    capability=descriptor.qualified_name,
                    provider_id=lease.provider_id,
                    served_locally=True,
                    request_id=str(call_id or ""),
                    execution_plane=plane,
                    runtime_kind=runtime,
                )
                if ok
                else capability_failure(
                    self._error_code(payload),
                    str((payload or {}).get("error") or f"{base} 未完成")[:300],
                    capability=descriptor.qualified_name,
                    provider_id=lease.provider_id,
                    details={"status": status},
                    execution_plane=plane,
                    runtime_kind=runtime,
                )
            ),
        )

    @staticmethod
    def _error_code(payload: Any) -> CapabilityErrorCode:
        """把客户端错误码原样接住（不被压成通用 FAILED）。"""
        raw = str((payload or {}).get("error_code") or "").upper()
        if raw:
            try:
                return CapabilityErrorCode(raw)
            except ValueError:
                pass
        return CapabilityErrorCode.FAILED


def capability_for_mcp_tool(tool_name: str) -> str | None:
    """MCP 工具名 → 能力名（executor 门禁用；``None`` = 本机动作/未知）。"""
    return capability_for_tool(tool_name)


__all__ = [
    "CAPABILITY_TOOL_MAP",
    "NEVER_FALLBACK_CAPABILITIES",
    "CapabilityDispatchAdapter",
    "DispatchOutcome",
    "adapt_to_mcp_tool",
    "capability_for_mcp_tool",
    "mcp_tool_for_capability",
    "select_lease",
]
