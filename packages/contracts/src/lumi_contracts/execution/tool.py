"""工具请求与工具规格（ToolSpec）。

* ``ToolRequest``：一次调用的命令。参数用**专属 Schema**（``input_schema``）承载，
  不退化成无约束 dict；
* ``ToolSpec``：注册项。除输入/输出类型外，还必须声明副作用、风险等级、数据敏感
  级别、权限、超时、重试与幂等策略——**高风险工具不会因为注册成功就自动获得写权限**。
* ``executor`` / ``serializer`` 用字符串或可调用对象表示；契约包不 import 业务实现。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumi_contracts.common.context import Sensitivity
from lumi_contracts.common.errors import ContractError, ContractErrorCode


class SideEffect(StrEnum):
    """副作用类型：决定是否需要审批与是否能并行。"""

    NONE = "none"
    STAGE_WRITE = "stage_write"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL = "external"
    PROCESS = "process"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff_ms: int = 0
    # 只对这些错误码重试；空集合表示"不自动重试"。
    retry_on: list[str] = Field(default_factory=list)


class IdempotencyPolicy(StrEnum):
    NATURAL_KEY = "natural_key"
    EXPLICIT_KEY = "explicit_key"
    NON_IDEMPOTENT = "non_idempotent"


class ToolRequest(BaseModel):
    """工具调用命令：参数与身份分离，身份只来自服务端上下文。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    namespace: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    # 关联标识（服务端注入）。
    call_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    # 幂等键：仅 non_idempotent/explicit_key 策略需要；由服务端生成。
    idempotency_key: str = ""
    # 审批指纹：由服务端计算并绑定"确切参数"，插件不得自填。
    approval_fingerprint: str = ""
    timeout_s: float | None = None

    @field_validator("arguments")
    @classmethod
    def _arguments_must_be_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ContractError(
                ContractErrorCode.INVALID_INPUT, "ToolRequest.arguments 必须是对象"
            )
        return value


class ToolSpec(BaseModel):
    """工具注册项：能力 + 治理声明。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    namespace: str = "lumi"
    version: str = "1.0.0"
    description: str = ""

    # 专属参数 Schema；不能是"无约束 dict"。
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # 非本地 Schema 的引用（如 ``mcp://lumi_client/workspace_read``）。
    # MCP 工具的输入定义**以 MCP 的 inputSchema 为唯一事实来源**，这里不再复制
    # 一份；需要参数表单/校验时按引用动态拉取（见 app.contracts.tools）。
    input_schema_ref: str = ""
    # 输出契约标识（形如 lumi.workspace_navigator.result@1）；可选。
    output_schema_name: str = ""
    output_schema_version: int = 1

    # 可调用实现（业务侧注入；契约包不 import 业务模块）。
    executor: Callable[..., Any] | None = None
    serializer: Callable[..., Any] | None = None
    # 声明式投影覆盖（``Projection`` 实例；空表示用 ProjectionRegistry 默认投影）。
    # 大多数工具不需要覆盖：默认投影已经能处理任意 payload；只有"读取类"结果
    # 需要给出可读分段时才显式声明。
    model_projection: Any = None
    ui_projection: Any = None

    # ── 治理声明（注册时必须给出）──
    side_effect: SideEffect = SideEffect.NONE
    risk_level: RiskLevel = RiskLevel.LOW
    data_sensitivity: Sensitivity = Sensitivity.INTERNAL
    required_permissions: tuple[str, ...] = ()
    requires_approval: bool = False
    allow_network: bool = False
    streaming_support: bool = False

    timeout_s: float = 0.0
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotency_policy: IdempotencyPolicy = IdempotencyPolicy.NATURAL_KEY

    # 投影用途的语义标签（不参与执行）。
    tags: tuple[str, ...] = ()
    internal: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name

    def validate_declaration(self) -> list[str]:
        """注册期自检：返回问题列表（空列表表示通过）。

        * 输入 Schema 必须是 object（或声明了 ``input_schema_ref`` 由外部提供）；
        * 有副作用的工具必须声明权限与审批语义；
        * 高风险/敏感工具不能声明为"无需审批"。
        """
        problems: list[str] = []
        if not self.name.strip():
            problems.append("缺少工具名")
        schema = self.input_schema
        if not self.input_schema_ref and (
            not isinstance(schema, dict) or schema.get("type", "object") != "object"
        ):
            problems.append("input_schema 必须是 object schema")
        writes = self.side_effect not in {SideEffect.NONE}
        if writes and not self.required_permissions:
            problems.append("有副作用的工具必须声明 required_permissions")
        if writes and not self.requires_approval and self.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            problems.append("高风险写工具必须声明 requires_approval")
        if self.data_sensitivity is Sensitivity.CREDENTIAL and self.allow_network:
            problems.append("凭据级数据不允许声明 allow_network")
        return problems

    def project(self, kind: str, result: Any) -> dict[str, Any]:
        """按声明投影结果：工具声明了专用投影就用它，否则交给注册表默认投影。

        投影失败**不得**影响结果交付：异常收敛为带 ``degraded`` 标记的保守视图。
        """
        from lumi_contracts.projections import default_projection_registry
        from lumi_contracts.projections.base import ProjectionKind

        declared = self.model_projection if str(kind) == "model" else self.ui_projection if str(kind) == "ui" else None
        if declared is not None and hasattr(declared, "project"):
            try:
                return dict(declared.project(result))
            except Exception as exc:  # noqa: BLE001 - 投影失败不能丢结果
                return {"kind": str(kind), "degraded": True, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
        try:
            return default_projection_registry().project(ProjectionKind(str(kind)), result)
        except Exception as exc:  # noqa: BLE001
            return {"kind": str(kind), "degraded": True, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def assert_valid(self) -> None:
        problems = self.validate_declaration()
        if problems:
            raise ContractError(
                ContractErrorCode.INVALID_CONTRACT,
                f"工具 {self.qualified_name} 治理声明不完整：{'；'.join(problems)}",
                details={"problems": problems},
            )


__all__ = [
    "IdempotencyPolicy",
    "RetryPolicy",
    "RiskLevel",
    "SideEffect",
    "ToolRequest",
    "ToolSpec",
]
