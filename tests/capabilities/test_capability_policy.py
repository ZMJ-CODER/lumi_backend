"""阶段 5 + 阶段 3 后端回归：Policy Pack、策略门禁、审批令牌绑定。

三件事必须同时成立，否则"策略"只是文案：

1. **包选择确定**：同一输入永远选到同一个包（会进 Job 快照，必须可复现）；
2. **判定真的拦得住**：需要审批的副作用没带令牌 → ``APPROVAL_REQUIRED``；
   令牌过期/参数变化 → ``APPROVAL_EXPIRED`` / ``APPROVAL_INVALID``；
3. **策略不能放宽**：``manual_commit`` / ``enterprise_audit`` 禁止 hybrid 切云端，
   调用方传 ``policy_allows_switch=True`` 也不能绕过。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    DataLocality,
    Deployment,
    ProviderHealth,
    SideEffectKind,
    capability_ok,
)

from app.agents.capabilities import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    AgentExecutionContext,
    CapabilityRegistry,
    PolicyPack,
    PolicyPackRegistry,
    PluginPolicyGuard,
    capability_catalog,
    capability_fingerprint,
    issue_approval_token,
    policy_packs,
    select_policy_id,
    validate_approval,
)
from app.agents.capabilities.broker.broker import CapabilityBroker
from app.services.capability_lease import CapabilityLeaseService


class _Provider:
    def __init__(self, *, provider_id: str, descriptors, deployment=Deployment.CLIENT) -> None:
        self._provider_id = provider_id
        self._descriptors = tuple(descriptors)
        self._deployment = deployment
        self.calls: list[CapabilityInvocation] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self) -> Deployment:
        return self._deployment

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def invoke(self, invocation, *, context) -> CapabilityResult:
        self.calls.append(invocation)
        return capability_ok(
            {"status": "ok"}, capability=invocation.qualified_capability, provider_id=self._provider_id
        )


def _context(**overrides) -> AgentExecutionContext:
    payload = {
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "device_id": "device-1",
    }
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


# ── 包定义与选择 ─────────────────────────────────────────────────


def test_builtin_policy_packs_encode_the_boundaries():
    assert set(policy_packs.ids()) == {
        "cost_saver",
        "high_precision",
        "manual_commit",
        "enterprise_audit",
    }
    manual = policy_packs.require("manual_commit")
    # 默认包：本地数据不出本机 + 写/执行必须人工提交。
    assert manual.allow_cloud_switch is False
    assert manual.needs_approval_for([SideEffectKind.WRITE]) is True
    assert manual.needs_approval_for([SideEffectKind.EXECUTE]) is True
    assert manual.needs_approval_for([SideEffectKind.READ]) is False
    audit = policy_packs.require("enterprise_audit")
    assert audit.audit_all is True
    assert audit.allow_streaming is False
    assert audit.max_inline_bytes < manual.max_inline_bytes
    cost = policy_packs.require("cost_saver")
    assert cost.allow_cloud_switch is True
    # 读操作在任何包下都不需要审批（否则每次读取都要用户点一次）。
    for pack in policy_packs.all():
        assert pack.needs_approval_for([SideEffectKind.READ]) is False
    # 客户端本机拒止的最小集：删除与对外发送可以本机否决。
    assert "delete" in manual.client_deny_side_effects


def test_select_policy_id_is_deterministic_and_conservative():
    # 显式指定优先
    assert select_policy_id(requested="enterprise_audit") == "enterprise_audit"
    # 未知指定被忽略（不回退到"随便一个"）
    assert select_policy_id(requested="nope") == "manual_commit"
    # 高危 → 精度优先
    assert select_policy_id(risk_level="HIGH_RISK") == "high_precision"
    # 审批模式
    assert select_policy_id(approval_mode="manual_commit") == "manual_commit"
    # 自动模式也不会为了省钱把本地数据搬到云上（只有对外发送才走 cost_saver）
    assert select_policy_id(approval_mode="auto_routine") == "manual_commit"
    assert (
        select_policy_id(approval_mode="auto_routine", side_effects=[SideEffectKind.EXTERNAL])
        == "cost_saver"
    )
    # 同一输入重复调用结果一致（要进快照）
    assert select_policy_id(approval_mode="manual_commit") == select_policy_id(
        approval_mode="manual_commit"
    )


def test_policy_pack_snapshot_is_auditable_and_json_safe():
    snapshot = policy_packs.require("high_precision").to_snapshot()
    assert snapshot["policy_id"] if "policy_id" in snapshot else snapshot["id"] == "high_precision"
    assert snapshot["approval_required_side_effects"] == sorted(
        {"delete", "execute", "external", "write"}
    )
    assert len(policy_packs.require("high_precision").digest()) == 64


def test_registry_rejects_duplicate_and_unknown_pack():
    registry = PolicyPackRegistry()
    with pytest.raises(ValueError):
        registry.register(PolicyPack(id="manual_commit"))
    with pytest.raises(KeyError):
        registry.require("nope")
    custom = registry.register(PolicyPack(id="custom", allow_cloud_switch=True), replace=False)
    assert custom.id == "custom"


# ── 审批令牌绑定 ─────────────────────────────────────────────────


def _invocation(**overrides) -> CapabilityInvocation:
    payload = {
        "capability": CAPABILITY_WORKSPACE_WRITE,
        "arguments": {"operation": "stage_write", "path": "src/app.py", "content": "x"},
        "request_id": "r1",
        "idempotency_key": "i1",
    }
    payload.update(overrides)
    return CapabilityInvocation(**payload)


def test_fingerprint_changes_with_arguments_and_scope():
    base = capability_fingerprint("workspace.write@1", {"operation": "commit"})
    assert base == capability_fingerprint("workspace.write@1", {"operation": "commit"})
    assert base != capability_fingerprint("workspace.write@1", {"operation": "rollback"})
    assert base != capability_fingerprint(
        "workspace.write@1", {"operation": "commit"}, scope={"workspace_id": "ws-2"}
    )
    # 执行期保留键不参与指纹（与既有工具审批一致）。
    assert base == capability_fingerprint(
        "workspace.write@1", {"operation": "commit", "_lumi_execution_policy": {"x": 1}}
    )


def test_validate_approval_reports_missing_expired_and_mismatched():
    invocation = _invocation()
    # 未审批
    verdict = validate_approval(invocation, approval_context=None)
    assert verdict.ok is False
    assert verdict.code == CapabilityErrorCode.APPROVAL_REQUIRED.value

    token = issue_approval_token(
        invocation.qualified_capability, invocation.arguments, scope=invocation.scope, now=100.0
    )
    # 指纹一致 → 通过
    ok = validate_approval(
        invocation,
        approval_context={"fingerprint": token["fingerprint"], "expires_at": token["expires_at"]},
        now=200.0,
    )
    assert ok.ok is True

    # 过期 → APPROVAL_EXPIRED（不能当成"没审批过"重来）
    expired = validate_approval(
        invocation,
        approval_context={"fingerprint": token["fingerprint"], "expires_at": 150.0},
        now=200.0,
    )
    assert expired.ok is False
    assert expired.code == CapabilityErrorCode.APPROVAL_EXPIRED.value

    # 参数变了 → APPROVAL_INVALID
    changed = _invocation(arguments={"operation": "commit", "path": "other.py"})
    mismatch = validate_approval(
        changed,
        approval_context={"fingerprint": token["fingerprint"], "expires_at": token["expires_at"]},
        now=200.0,
    )
    assert mismatch.ok is False
    assert mismatch.code == CapabilityErrorCode.APPROVAL_INVALID.value


def test_issued_token_carries_binding_and_single_use_default():
    token = issue_approval_token("workspace.write@1", {"operation": "commit"}, now=1000.0)
    assert token["max_uses"] == 1
    assert token["expires_at"] > token["issued_at"]
    assert token["fingerprint"] == capability_fingerprint(
        "workspace.write@1", {"operation": "commit"}
    )


# ── 门禁判定 ─────────────────────────────────────────────────────


def test_guard_requires_approval_only_for_write_like_side_effects():
    guard = PluginPolicyGuard(pack=policy_packs.require("manual_commit"))
    read = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    write = capability_catalog.require(CAPABILITY_WORKSPACE_WRITE)
    assert guard.evaluate(read).needs_approval is False
    assert guard.evaluate(write).needs_approval is True
    # 只读能力即使不带审批也放行
    _verdict, failure = guard.authorize(read, _invocation(capability=CAPABILITY_WORKSPACE_READ))
    assert failure is None
    # 写能力缺审批 → 结构化 APPROVAL_REQUIRED，且带"重新发起审批"的提示
    verdict, failure = guard.authorize(write, _invocation())
    assert failure is not None
    assert failure.error_code == CapabilityErrorCode.APPROVAL_REQUIRED.value
    assert verdict.allowed is False
    assert failure.needs_install is False


def test_guard_allows_write_when_approval_matches():
    guard = PluginPolicyGuard(pack=policy_packs.require("manual_commit"))
    write = capability_catalog.require(CAPABILITY_WORKSPACE_WRITE)
    invocation = _invocation()
    token = issue_approval_token(
        invocation.qualified_capability, invocation.arguments, scope=invocation.scope
    )
    verdict, failure = guard.authorize(
        write,
        invocation,
        approval_context={"fingerprint": token["fingerprint"], "expires_at": token["expires_at"]},
    )
    assert failure is None
    assert verdict.needs_approval is True
    assert verdict.allowed is True


def test_guard_cloud_switch_flag_follows_the_pack():
    manual = PluginPolicyGuard(pack=policy_packs.require("manual_commit"))
    cost = PluginPolicyGuard(pack=policy_packs.require("cost_saver"))
    assert manual.cloud_switch_allowed() is False
    assert cost.cloud_switch_allowed() is True


# ── 审批结论 ↔ 门禁上下文（阶段 3 后端接线）────────────────────────


def test_capability_approval_round_trip_through_job_and_node():
    """审批通过 → 记录到 job.routing → 搬到节点 → Broker 门禁上下文可用。"""
    from app.agents.capabilities.policy.approvals import (
        approval_snapshot,
        capability_approval_context,
        carry_approval_to_step,
        node_approval_context,
        record_capability_approval,
    )
    from app.agents.orchestration.models import Job, TaskNode

    job = Job(job_id="j1", user_id="u1", request="r", scene="office")
    node = TaskNode(id="s1", name="写盘", agent="w1")
    invocation = _invocation()
    token = issue_approval_token(
        invocation.qualified_capability, invocation.arguments, scope=invocation.scope
    )
    record_capability_approval(
        job,
        capability=invocation.qualified_capability,
        fingerprint=token["fingerprint"],
        expires_at=token["expires_at"],
        approved_by="u1",
    )
    # 只写指纹/时间，不写参数
    stored = job.routing["capability_approvals"][invocation.qualified_capability]
    assert "arguments" not in stored and "content" not in str(stored)

    context = capability_approval_context(job, invocation.qualified_capability)
    assert context is not None
    verdict, failure = PluginPolicyGuard(
        pack=policy_packs.require("manual_commit")
    ).authorize(
        capability_catalog.require(CAPABILITY_WORKSPACE_WRITE),
        invocation,
        approval_context=context,
    )
    assert failure is None and verdict.allowed is True

    # 搬到节点（worker 侧发起能力调用时读它）
    assert carry_approval_to_step(job, node, capability=invocation.qualified_capability)
    assert node_approval_context(node, invocation.qualified_capability)["fingerprint"] == token["fingerprint"]
    # 既有工具级审批元数据不受影响（各走各的）
    assert "confirmed_tool_calls" not in (node.metadata or {})

    assert approval_snapshot(job)[0]["expired"] is False


def test_expired_capability_approval_is_not_reused():
    from app.agents.capabilities.policy.approvals import (
        approval_snapshot,
        capability_approval_context,
        record_capability_approval,
    )
    from app.agents.orchestration.models import Job

    job = Job(job_id="j1", user_id="u1", request="r", scene="office")
    record_capability_approval(
        job, capability="workspace.write@1", fingerprint="fp", expires_at=100.0, now=50.0
    )
    # 过期后不再作为授权依据……
    assert capability_approval_context(job, "workspace.write@1", now=200.0) is None
    # ……但快照仍保留"曾经批准过"的审计事实并标记过期。
    assert approval_snapshot(job, now=200.0)[0]["expired"] is True
    # 未记录过的能力永远是 None（不能凭空授权）
    assert capability_approval_context(job, "code.execute@1") is None


# ── Broker 集成 ──────────────────────────────────────────────────


def _broker_with(*, read_only: bool):
    registry = CapabilityRegistry()
    descriptor = capability_catalog.require(
        CAPABILITY_WORKSPACE_READ if read_only else CAPABILITY_WORKSPACE_WRITE
    )
    provider = _Provider(provider_id="lumi.local.workspace", descriptors=(descriptor,))
    leases = CapabilityLeaseService(registry=registry, provider_resolver=lambda _lease: provider)
    broker = CapabilityBroker(registry=registry, leases=leases)

    async def setup():
        await leases.register(
            provider_id=provider.provider_id,
            capabilities=[{"capability": descriptor.name, "contract_version": 1}],
            user_id="u1",
            device_id="device-1",
            workspace_id="ws-1",
            conversation_id="c1",
            ttl_seconds=60,
            health_status=ProviderHealth.HEALTHY.value,
        )
        leases.sync_registry()

    asyncio.run(setup())
    return broker, provider


def test_broker_blocks_write_without_approval():
    broker, provider = _broker_with(read_only=False)
    result = asyncio.run(
        broker.invoke(_invocation(), context=_context(), policy_id="manual_commit")
    )
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.APPROVAL_REQUIRED.value
    assert provider.calls == [], "未审批的能力绝不能被 Provider 执行"


def test_broker_executes_write_with_matching_approval():
    broker, provider = _broker_with(read_only=False)
    invocation = _invocation()
    token = issue_approval_token(
        invocation.qualified_capability, invocation.arguments, scope=invocation.scope
    )
    result = asyncio.run(
        broker.invoke(
            invocation,
            context=_context(),
            policy_id="manual_commit",
            approval_context={
                "fingerprint": token["fingerprint"],
                "expires_at": token["expires_at"],
            },
        )
    )
    assert result.ok is True
    assert len(provider.calls) == 1


def test_broker_read_path_is_unaffected_by_approval_policy():
    broker, provider = _broker_with(read_only=True)
    invocation = _invocation(
        capability=CAPABILITY_WORKSPACE_READ, arguments={"action": "read", "path": "a.py"}
    )
    result = asyncio.run(
        broker.invoke(invocation, context=_context(), policy_id="manual_commit")
    )
    assert result.ok is True
    assert len(provider.calls) == 1


def test_broker_cannot_be_talked_into_cloud_switch_by_the_caller():
    """manual_commit 禁止切云端；调用方传 policy_allows_switch 也不能绕过。"""
    descriptor = capability_catalog.require(CAPABILITY_ARTIFACT_CREATE).model_copy(
        update={"data_locality": DataLocality.HYBRID}
    )
    registry = CapabilityRegistry()
    server = _Provider(
        provider_id="lumi.server.artifact", descriptors=(descriptor,), deployment=Deployment.SERVER
    )
    leases = CapabilityLeaseService(registry=registry, provider_resolver=lambda _lease: server)

    async def setup():
        await leases.register(
            provider_id=server.provider_id,
            capabilities=[{"capability": descriptor.name, "contract_version": 1}],
            user_id="u1",
            # 租约要能匹配调用方的绑定（否则严格工作区匹配会直接排除它）。
            device_id="device-1",
            workspace_id="ws-1",
            deployment=Deployment.SERVER.value,
            ttl_seconds=60,
            health_status=ProviderHealth.HEALTHY.value,
        )
        leases.sync_registry()

    asyncio.run(setup())
    broker = CapabilityBroker(
        registry=registry,
        leases=leases,
        policy_guard=PluginPolicyGuard(pack=policy_packs.require("manual_commit")),
    )
    invocation = CapabilityInvocation(
        capability=CAPABILITY_ARTIFACT_CREATE, arguments={"kind": "document"}, request_id="r9"
    )
    # manual_commit 不允许切云端：调用方传 policy_allows_switch=True 也不能绕过。
    # 结果是结构化失败，而不是"悄悄在服务端执行"。
    strict_result = asyncio.run(
        broker.invoke(
            invocation,
            context=_context(),
            policy_id="manual_commit",
            policy_allows_switch=True,
        )
    )
    assert strict_result.ok is False
    assert strict_result.error_code in {
        CapabilityErrorCode.CAPABILITY_MISSING.value,
        CapabilityErrorCode.APPROVAL_REQUIRED.value,
        CapabilityErrorCode.PROVIDER_OFFLINE.value,
    }
    # 换成允许切云端的包（cost_saver）后，同一调用能走到 Provider —— 证明上面失败
    # 的原因确实是策略收紧，而不是"服务端根本没有这个能力"。
    permissive_broker = CapabilityBroker(
        registry=registry,
        leases=leases,
        policy_guard=PluginPolicyGuard(pack=policy_packs.require("cost_saver")),
    )
    allowed = asyncio.run(
        permissive_broker.invoke(
            CapabilityInvocation(
                capability=CAPABILITY_ARTIFACT_CREATE,
                arguments={"kind": "document"},
                request_id="r10",
            ),
            context=_context(),
            policy_id="cost_saver",
            policy_allows_switch=True,
        )
    )
    assert allowed.ok is True
    assert len(server.calls) == 1
