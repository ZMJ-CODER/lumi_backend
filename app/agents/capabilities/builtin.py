"""阶段 1：内置 Provider —— 把**已有**能力包成统一契约（行为不变）。

三条边界，写清楚是为了避免后面的阶段把这里改歪：

1. **不新增能力、不改行为**：每个 Provider 只是把既有实现（`WorkspaceNavigatorService`、
   本地沙箱、`render_document`）按 ``CapabilityInvocation`` 调用一次，把结果折成
   ``CapabilityResult``。它不做审批判定、不做模型路由、不改参数语义；
2. **授权只来自上下文**：``workspace_id`` / ``project_id`` 一律取
   ``AgentExecutionContext``（服务端授权事实），**忽略**调用方传的同名字段——
   与 `executor.py` 里"模型传值被忽略"的既有规则一致；
3. **不假装本地能力能在服务端跑**：客户端能力（workspace.*、code.execute、
   git.operations）在这里用"转发占位"实现，服务端内联执行只对 ``artifact.create``
   这种 ``cloud`` 能力开放。

因此这一层是**可逆**的：即使 Broker 全部回退，既有执行路径也不受影响。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    DataLocality,
    Deployment,
    ExecutionPlane,
    RuntimeKind,
    TrustLevel,
    capability_failure,
    capability_ok,
)

from app.agents.capabilities.catalog import (
    CAPABILITY_ARTIFACT_CREATE,
    CAPABILITY_CODE_EXECUTE,
    CAPABILITY_CODE_SCAN,
    CAPABILITY_GIT_OPERATIONS,
    CAPABILITY_WORKSPACE_DELETE,
    CAPABILITY_WORKSPACE_EDIT,
    CAPABILITY_WORKSPACE_MOVE,
    CAPABILITY_WORKSPACE_READ,
    CAPABILITY_WORKSPACE_WRITE,
    CapabilityCatalog,
    capability_catalog,
)
from app.agents.capabilities.context import AgentExecutionContext
from app.agents.capabilities.registry import CapabilityRegistry, capability_registry

#: 内置 Provider 的 provider_id（审计快照里会出现，改名等于改审计记录）。
PROVIDER_SERVER_ARTIFACT = "lumi.server.artifact"
PROVIDER_CLIENT_WORKSPACE = "lumi.local.workspace"
PROVIDER_CLIENT_CODE = "lumi.local.code"
PROVIDER_CLIENT_GIT = "lumi.local.git"
#: 需要客户端承载的能力在服务端的 Provider id（不在本进程执行，只表达"由客户端做"）。
PROVIDER_CLIENT_REMOTE = "lumi.client.forwarder"


class ServerArtifactProvider:
    """``artifact.create``：服务端渲染产物（cloud，客户端离线也能跑）。"""

    def __init__(self, *, catalog: CapabilityCatalog | None = None) -> None:
        self._descriptor = (catalog or capability_catalog).require(CAPABILITY_ARTIFACT_CREATE)

    @property
    def provider_id(self) -> str:
        return PROVIDER_SERVER_ARTIFACT

    @property
    def deployment(self) -> Deployment:
        return Deployment.SERVER

    @property
    def execution_plane(self) -> ExecutionPlane:
        """服务端内置 Provider：在 API 进程内执行（没有独立 Worker）。"""
        return ExecutionPlane.SERVER

    @property
    def runtime_kind(self) -> RuntimeKind:
        return RuntimeKind.IN_PROCESS

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return (self._descriptor,)

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
    ) -> CapabilityResult:
        from app.services.document_renderer import render_document
        from app.services.office_docs import generic_outputs_dir

        args = dict(invocation.arguments or {})
        args.setdefault("kind", "document")
        # 用户与产物的落点由服务端决定，调用方不能指定目录。
        user_id = context.user_id
        if not user_id:
            return capability_failure(
                CapabilityErrorCode.PERMISSION_DENIED,
                "需要登录后才能生成产物",
                capability=invocation.qualified_capability,
                provider_id=self.provider_id,
            )
        try:
            output_dir = generic_outputs_dir(user_id, context.conversation_id or "default")
            path = Path(render_document(args, Path(output_dir)))
        except Exception as exc:  # noqa: BLE001 - 渲染失败要变成结构化错误
            logger.warning("[capability] artifact.create 渲染失败: {}", str(exc)[:200])
            return capability_failure(
                "DOCUMENT_RENDER_FAILED",
                f"产物生成失败：{str(exc)[:200]}",
                capability=invocation.qualified_capability,
                provider_id=self.provider_id,
            )
        size = path.stat().st_size if path.exists() else 0
        # 产物引用必须带稳定 ref_id（否则投影层会丢弃它，前端拿不到"可下载"入口）。
        ref_id = f"{invocation.request_id or invocation.idempotency_key or 'artifact'}:{path.name}"
        return capability_ok(
            {
                "status": "success",
                "artifacts": [{"name": path.name, "size": size, "generic": True}],
            },
            capability=invocation.qualified_capability,
            provider_id=self.provider_id,
            artifact_refs=[
                {"ref_id": ref_id, "name": path.name, "media_type": "", "size": size}
            ],
            sensitivity="internal",
            served_locally=False,
            execution_plane=ExecutionPlane.SERVER,
            runtime_kind=RuntimeKind.IN_PROCESS,
        )


class ClientForwardingProvider:
    """客户端能力的**服务端侧声明**：只登记能力，不在服务端执行。

    ``workspace.read`` / ``code.execute`` / ``git.operations`` 的真实执行在用户设备
    （Electron Provider）。服务端这个对象只保证：

    * 能力在目录里可查、可路由（知道它属于客户端）；
    * 服务端**明确拒绝**就地执行——绝不能因为"客户端不在线"就偷偷在服务端跑本地读取。
    """

    def __init__(
        self,
        *,
        provider_id: str,
        descriptors: tuple[CapabilityDescriptor, ...],
        catalog: CapabilityCatalog | None = None,
        runtime_kind: RuntimeKind = RuntimeKind.IN_PROCESS,
    ) -> None:
        self._provider_id = provider_id
        self._descriptors = descriptors
        #: 客户端能力的实现跑在用户设备上；默认是设备主进程（进程内），
        #: 平台侧插件宿主可以声明为 ``worker``。
        self._runtime_kind = runtime_kind

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def deployment(self) -> Deployment:
        return Deployment.CLIENT

    @property
    def execution_plane(self) -> ExecutionPlane:
        return ExecutionPlane.CLIENT

    @property
    def runtime_kind(self) -> RuntimeKind:
        return self._runtime_kind

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def invoke(
        self,
        invocation: CapabilityInvocation,
        *,
        context: AgentExecutionContext,
    ) -> CapabilityResult:
        return capability_failure(
            CapabilityErrorCode.PROVIDER_OFFLINE,
            f"{invocation.qualified_capability} 只能由用户设备上的 Provider 执行",
            capability=invocation.qualified_capability,
            provider_id=self.provider_id,
            retryable=True,
            execution_plane=ExecutionPlane.CLIENT,
            runtime_kind=self._runtime_kind,
        )


def builtin_providers(
    *, catalog: CapabilityCatalog | None = None
) -> tuple[Any, ...]:
    """全部内置 Provider（顺序稳定，便于断言）。"""
    catalog = catalog or capability_catalog
    return (
        ServerArtifactProvider(catalog=catalog),
        ClientForwardingProvider(
            provider_id=PROVIDER_CLIENT_WORKSPACE,
            descriptors=(
                catalog.require(CAPABILITY_WORKSPACE_READ),
                catalog.require(CAPABILITY_WORKSPACE_WRITE),
            ),
            catalog=catalog,
        ),
        ClientForwardingProvider(
            provider_id=PROVIDER_CLIENT_CODE,
            descriptors=(
                catalog.require(CAPABILITY_CODE_EXECUTE),
                # 代码扫描与执行同属客户端代码域（**数据**都依赖客户端在线），归到同一 Provider。
                # 注意：扫描的**解析实现**在服务端聚合服务里（code_structure 纯函数），
                # 客户端只登记描述、不广告 code.scan 租约；这里声明的是"数据域"而非执行位置。
                catalog.require(CAPABILITY_CODE_SCAN),
            ),
            catalog=catalog,
        ),
        ClientForwardingProvider(
            provider_id=PROVIDER_CLIENT_GIT,
            descriptors=(catalog.require(CAPABILITY_GIT_OPERATIONS),),
            catalog=catalog,
        ),
    )


def register_builtin_providers(
    *, registry: CapabilityRegistry | None = None, catalog: CapabilityCatalog | None = None
) -> list[str]:
    """注册内置 Provider，返回已注册的 provider_id 列表（幂等）。

    客户端能力的 Provider 注册在 **CLIENT** 侧（它们的实现位置），服务端不持有实现；
    重复调用是覆盖而不是叠加（``CapabilityRegistry.register`` 的既有语义）。
    """
    target = registry or capability_registry
    providers = builtin_providers(catalog=catalog)
    for provider in providers:
        target.register(
            provider,
            descriptors=provider.descriptors,
            deployment=provider.deployment,
            trust_level=TrustLevel.BUILTIN,
            provider_version="1.0.0",
            # Provider 自述的执行位置/运行方式优先（缺省按 deployment 推导）。
            execution_plane=getattr(provider, "execution_plane", None),
            runtime_kind=getattr(provider, "runtime_kind", None),
        )
    return [provider.provider_id for provider in providers]


#: ``artifact.create`` 是唯一服务端可直接执行的能力（与目录声明一致）。
SERVER_INLINE_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_ARTIFACT_CREATE})

#: 客户端 MCP 原子工具 ↔ 能力的路由表（与客户端 ``capability_bridge.tools`` 对齐）。
#:
#: **派发必须按租约**：MCP 原子工具直接调本机实现、不过健康门禁，因此只按"工具可达"
#: 派发会让健康隔离与撤销通道失效（租约被摘除后同名工具仍能读本机文件）。
#: ``None`` 表示本机动作（打开应用/浏览器、向用户澄清），不参与租约。
TOOL_CAPABILITY_MAP: dict[str, str | None] = {
    "workspace_navigator": CAPABILITY_WORKSPACE_READ,
    "workspace_catalog": CAPABILITY_WORKSPACE_READ,
    "workspace_list": CAPABILITY_WORKSPACE_READ,
    "workspace_stat": CAPABILITY_WORKSPACE_READ,
    "workspace_read": CAPABILITY_WORKSPACE_READ,
    "workspace_search": CAPABILITY_WORKSPACE_READ,
    "workspace_content_extract": CAPABILITY_WORKSPACE_READ,
    # 代码骨架扫描：与读取同域但**独立能力**（模型可用它替代"整篇读代码"）。
    "workspace_code_scan": CAPABILITY_CODE_SCAN,
    "workspace_write": CAPABILITY_WORKSPACE_WRITE,
    "workspace_stage_write": CAPABILITY_WORKSPACE_WRITE,
    "workspace_stage_delete": CAPABILITY_WORKSPACE_WRITE,
    "workspace_rollback": CAPABILITY_WORKSPACE_WRITE,
    # 统一操作契约（OperationResult）：编辑/移动/删除各自独立声明，
    # 因为审批与版本前置条件不同（见 app/contracts/operations）。
    "workspace_edit": CAPABILITY_WORKSPACE_EDIT,
    "code.edit": CAPABILITY_WORKSPACE_EDIT,
    "workspace_move": CAPABILITY_WORKSPACE_MOVE,
    "workspace_delete": CAPABILITY_WORKSPACE_DELETE,
    "workspace_diff": CAPABILITY_GIT_OPERATIONS,
    "workspace_commit": CAPABILITY_GIT_OPERATIONS,
    "sandbox_prepare": CAPABILITY_CODE_EXECUTE,
    "sandbox_run": CAPABILITY_CODE_EXECUTE,
    "sandbox_output_read": CAPABILITY_CODE_EXECUTE,
    "sandbox_reset": CAPABILITY_CODE_EXECUTE,
    # 本机动作：不参与租约（打开应用/浏览器、向用户澄清）。
    "desktop_open_app": None,
    "desktop_open_url": None,
    "user_clarify": None,
}


def capability_for_tool(tool_name: str) -> str | None:
    """工具名 → 能力名（``None`` = 本机动作或未知工具）。

    兼容三种写法（都是生产里真实出现的）：

    * 裸名 ``workspace_navigator``（内部调用/路由表对照）；
    * 限定名 ``mcp__lumi_pc__workspace_navigator``（模型看到的 MCP 工具名）；
    * 带命名空间的 ``server.tool``。

    只做**后缀匹配到已知工具**，绝不按关键词猜（猜错的代价是把写操作当只读派发）。
    """
    requested = str(tool_name or "").strip()
    if not requested:
        return None
    if requested in TOOL_CAPABILITY_MAP:
        return TOOL_CAPABILITY_MAP[requested]
    suffix = requested.split("__")[-1]
    if suffix in TOOL_CAPABILITY_MAP:
        return TOOL_CAPABILITY_MAP[suffix]
    tail = requested.rsplit(".", 1)[-1]
    if tail in TOOL_CAPABILITY_MAP:
        return TOOL_CAPABILITY_MAP[tail]
    return None


def capability_requires_client(capability: str) -> bool:
    """该能力是否**必须**由客户端执行（本地数据不允许在服务端跑）。"""
    descriptor = capability_catalog.get(capability)
    if descriptor is None:
        return True
    return descriptor.data_locality is DataLocality.LOCAL_ONLY


__all__ = [
    "ClientForwardingProvider",
    "PROVIDER_CLIENT_CODE",
    "PROVIDER_CLIENT_GIT",
    "PROVIDER_CLIENT_REMOTE",
    "PROVIDER_CLIENT_WORKSPACE",
    "PROVIDER_SERVER_ARTIFACT",
    "SERVER_INLINE_CAPABILITIES",
    "ServerArtifactProvider",
    "TOOL_CAPABILITY_MAP",
    "builtin_providers",
    "capability_for_tool",
    "capability_requires_client",
    "register_builtin_providers",
]
