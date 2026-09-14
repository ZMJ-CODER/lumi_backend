"""阶段 1 回归：现有能力 Provider 化（目录 + 注册表 + 一致性约束）。

第一阶段的目标是"现有能力能用统一契约描述与注册"，并且**行为不变**。因此这里
断言的重点不是新功能，而是约束不被绕过：

1. 目录把现有实现映射到能力，且每个能力都有输入 Schema（不是万能字符串字典）；
2. 本地能力（``local_only``）**不能**注册到服务端 —— 否则等于把本地读取偷偷搬到
   云端执行；
3. 注册声明必须与目录一致：不得降级声明本地性、去掉副作用或放宽本机确认；
4. 找不到 Provider 时返回结构化 ``CAPABILITY_MISSING``（不是异常、不是"工具失败"）；
5. 多个候选 Provider 时必须由调用方先按会话/设备选定，注册表不猜；
6. 服务端可直接执行的能力集合与 ``data_locality`` 自洽。
"""

from __future__ import annotations

import asyncio

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
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_CODE_SCAN,
    CAPABILITY_GIT_OPERATIONS,
    CAPABILITY_WORKSPACE_DELETE,
    CAPABILITY_WORKSPACE_EDIT,
    CAPABILITY_WORKSPACE_MOVE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    IMPLEMENTATION_MAP,
    SERVER_EXECUTABLE_CAPABILITIES,
    WORKSPACE_OPERATION_CAPABILITIES,
    AgentExecutionContext,
    CapabilityCatalog,
    CapabilityRegistry,
    capability_catalog,
)


def _context(**overrides) -> AgentExecutionContext:
    payload = {
        "user_id": "u1",
        "conversation_id": "c1",
        "workspace_id": "ws-1",
        "device_id": "device-1",
        "project_ids": ["proj-1"],
    }
    payload.update(overrides)
    return AgentExecutionContext.from_metadata(**payload)


class _FakeProvider:
    """最小 Provider：记录收到的调用，返回成功结果。"""

    def __init__(self, *, provider_id: str, descriptors, deployment=Deployment.SERVER) -> None:
        self._provider_id = provider_id
        self._descriptors = tuple(descriptors)
        self._deployment = deployment
        self.calls: list[CapabilityInvocation] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self):
        return self._deployment

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def invoke(self, invocation, *, context) -> CapabilityResult:
        self.calls.append(invocation)
        return capability_ok(
            {"echo": dict(invocation.arguments)},
            capability=invocation.qualified_capability,
            provider_id=self._provider_id,
        )


# ── (1) 目录 ─────────────────────────────────────────────────────


def test_catalog_declares_the_first_batch_with_typed_schemas():
    names = set(capability_catalog.names())
    assert names == {
        CAPABILITY_WORKSPACE_READ,
        CAPABILITY_WORKSPACE_WRITE,
        CAPABILITY_WORKSPACE_EDIT,
        CAPABILITY_WORKSPACE_MOVE,
        CAPABILITY_WORKSPACE_DELETE,
        CAPABILITY_CODE_EXECUTE,
        CAPABILITY_CODE_SCAN,
        CAPABILITY_GIT_OPERATIONS,
        CAPABILITY_ARTIFACT_CREATE,
    }
    for descriptor in capability_catalog.all():
        # 结构化 Schema（不接受 map<string,string> 式的无类型参数）。
        assert descriptor.input_schema.get("type") == "object"
        assert descriptor.output_schema.get("type") == "object"
        assert descriptor.summary.strip()
    read = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    assert read.data_locality is DataLocality.LOCAL_ONLY
    assert read.streamable is True
    assert read.qualified_name == "workspace.read@1"
    # code.scan 与 workspace.read 同域：只读、本地、免审批，所以不联网、不进服务端。
    scan = capability_catalog.require(CAPABILITY_CODE_SCAN)
    assert scan.qualified_name == "code.scan@1"
    assert scan.data_locality is DataLocality.LOCAL_ONLY
    assert {str(item) for item in scan.side_effects} == {"read"}
    assert scan.needs_local_confirmation is False
    # 四个操作能力必须共用同一份输出契约（OperationResult 形状，前端只解析一套）。
    operation_outputs = [
        capability_catalog.require(name).output_schema
        for name in WORKSPACE_OPERATION_CAPABILITIES
    ]
    assert all(schema == operation_outputs[0] for schema in operation_outputs)
    assert "status" in operation_outputs[0]["properties"]
    assert "no_change" in operation_outputs[0]["properties"]["status"]["enum"]
    assert "already_absent" in operation_outputs[0]["properties"]["status"]["enum"]
    # 目录快照可供 Job 快照/审计使用
    snapshot = capability_catalog.to_snapshot()
    assert {row["capability"] for row in snapshot} == {
        "workspace.read@1", "workspace.write@1", "workspace.edit@1",
        "workspace.move@1", "workspace.delete@1",
        "code.execute@1", "code.scan@1",
        "git.operations@1", "artifact.create@1",
    }


def test_existing_implementations_map_to_declared_capabilities():
    """现有实现必须全部映射到已声明能力（迁移期反查的基础）。"""
    for implementation, capability in IMPLEMENTATION_MAP.items():
        descriptor = capability_catalog.descriptor_for_implementation(implementation)
        assert descriptor is not None, f"{implementation} 没有对应能力"
        assert descriptor.name == capability
    assert capability_catalog.descriptor_for_implementation("unknown_thing") is None


def test_server_executable_set_matches_locality():
    """服务端可直接执行的能力必须是 cloud；本地能力不得混进来。"""
    for name in SERVER_EXECUTABLE_CAPABILITIES:
        descriptor = capability_catalog.require(name)
        assert descriptor.data_locality is DataLocality.CLOUD
    for descriptor in capability_catalog.all():
        if descriptor.data_locality is DataLocality.LOCAL_ONLY:
            assert descriptor.name not in SERVER_EXECUTABLE_CAPABILITIES


def test_write_capabilities_require_approval_and_local_confirmation():
    write = capability_catalog.require(CAPABILITY_WORKSPACE_WRITE)
    assert {str(item) for item in write.side_effects} == {"write"}
    assert write.needs_local_confirmation is True
    # 四个操作能力的副作用各不相同，必须分别声明（审批档位/风险提示都依赖它）。
    assert {str(item) for item in capability_catalog.require(CAPABILITY_WORKSPACE_EDIT).side_effects} == {"write"}
    assert {str(item) for item in capability_catalog.require(CAPABILITY_WORKSPACE_MOVE).side_effects} == {"write", "delete"}
    assert {str(item) for item in capability_catalog.require(CAPABILITY_WORKSPACE_DELETE).side_effects} == {"delete"}
    for name in WORKSPACE_OPERATION_CAPABILITIES:
        assert capability_catalog.require(name).needs_local_confirmation is True, name
        assert capability_catalog.require(name).data_locality is DataLocality.LOCAL_ONLY, name
    execute = capability_catalog.require(CAPABILITY_CODE_EXECUTE)
    assert SideEffectKind.EXECUTE in execute.side_effects
    assert execute.needs_local_confirmation is True
    # 只读能力不需要本机确认（否则每次读取都要用户点一次）。
    assert capability_catalog.require(CAPABILITY_WORKSPACE_READ).needs_local_confirmation is False


# ── (2)(3) 注册约束 ──────────────────────────────────────────────


def test_local_capability_cannot_be_registered_on_server_side():
    """本地读取绝不允许注册成服务端 Provider（那是把本地数据搬到云端）。"""
    registry = CapabilityRegistry()
    provider = _FakeProvider(
        provider_id="sneaky.server.reader",
        descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
        deployment=Deployment.SERVER,
    )
    try:
        registry.register(provider)
    except ValueError as exc:
        assert "数据本地性" in str(exc)
    else:  # pragma: no cover - 必须拒绝
        raise AssertionError("本地能力被注册到服务端成功，属于越权隐患")
    # 客户端侧注册成功
    registry.register(
        _FakeProvider(
            provider_id="lumi.local.workspace",
            descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
            deployment=Deployment.CLIENT,
        )
    )
    assert [item.provider_id for item in registry.registrations(CAPABILITY_WORKSPACE_READ)] == [
        "lumi.local.workspace"
    ]


def test_registration_must_match_catalog_declaration():
    """注册方不得降级声明（本地性/副作用/本机确认）。"""
    registry = CapabilityRegistry()
    declared = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    downgraded = declared.model_copy(update={"data_locality": DataLocality.CLOUD})
    try:
        registry.register(
            _FakeProvider(
                provider_id="downgraded",
                descriptors=(downgraded,),
                deployment=Deployment.CLIENT,
            )
        )
    except ValueError as exc:
        assert "不一致" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("降级声明被接受")

    no_approval = capability_catalog.require(CAPABILITY_WORKSPACE_WRITE).model_copy(
        update={"needs_local_confirmation": False}
    )
    try:
        registry.register(
            _FakeProvider(
                provider_id="no-confirm",
                descriptors=(no_approval,),
                deployment=Deployment.CLIENT,
            )
        )
    except ValueError as exc:
        assert "needs_local_confirmation" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("放宽本机确认被接受")


def test_catalog_rejects_duplicate_capability_declaration():
    declared = capability_catalog.require(CAPABILITY_WORKSPACE_READ)
    try:
        CapabilityCatalog((declared, declared))
    except ValueError as exc:
        assert "重复声明" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复能力声明被接受")


# ── (4)(5) 调用与候选选择 ────────────────────────────────────────


def test_invoke_returns_structured_missing_capability():
    registry = CapabilityRegistry()

    async def scenario() -> CapabilityResult:
        return await registry.invoke(
            CapabilityInvocation(capability=CAPABILITY_WORKSPACE_READ, request_id="r1"),
            context=_context(),
        )

    result = asyncio.run(scenario())
    assert result.ok is False
    assert result.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value
    # 前端据此提示"安装/启用 Provider"，而不是报"工具调用失败"。
    assert result.needs_install is True
    assert "Provider" in (result.error.suggested_action if result.error else "")


def test_invoke_requires_unambiguous_provider_and_passes_context():
    registry = CapabilityRegistry()
    first = _FakeProvider(
        provider_id="device-a",
        descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
        deployment=Deployment.CLIENT,
    )
    second = _FakeProvider(
        provider_id="device-b",
        descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
        deployment=Deployment.CLIENT,
    )
    registry.register(first)
    registry.register(second)

    invocation = CapabilityInvocation(
        capability=CAPABILITY_WORKSPACE_READ, arguments={"action": "read"}, request_id="r2"
    )

    async def ambiguous():
        return await registry.invoke(invocation, context=_context())

    result = asyncio.run(ambiguous())
    assert result.error_code == CapabilityErrorCode.CAPABILITY_UNAVAILABLE.value
    assert sorted(result.error.details["candidates"]) == ["device-a", "device-b"]
    assert first.calls == [] and second.calls == []

    async def chosen():
        return await registry.invoke(invocation, context=_context(), provider_id="device-b")

    result = asyncio.run(chosen())
    assert result.ok is True
    assert result.provider_id == "device-b"
    assert second.calls and not first.calls
    # Provider 拿到的是结构化参数（不是字符串字典）。
    assert second.calls[0].arguments == {"action": "read"}


def test_unregister_and_health_updates_are_lease_driven():
    """租约过期/客户端断开时摘除能力，健康状态按 Provider 批量更新。"""
    registry = CapabilityRegistry()
    registration = registry.register(
        _FakeProvider(
            provider_id="device-a",
            descriptors=(capability_catalog.require(CAPABILITY_WORKSPACE_READ),),
            deployment=Deployment.CLIENT,
        )
    )
    assert registration.health_status is ProviderHealth.UNKNOWN
    assert registry.update_health("device-a", ProviderHealth.OFFLINE) == 1
    assert registry.providers()[0].health_status is ProviderHealth.OFFLINE
    assert registry.unregister("device-a") == 1
    assert registry.registrations(CAPABILITY_WORKSPACE_READ) == []
    assert registry.unregister("device-a") == 0
