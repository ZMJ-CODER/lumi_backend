"""``CapabilityDescriptor``：一个能力"能做什么"的完整声明（阶段 0 冻结）。

能力名 + 契约版本是**唯一的寻址键**（``workspace.read@1``）：模型/技能只依赖能力名，
不依赖 Provider 叫什么、跑在哪一侧。描述符自身携带输入/输出 JSON Schema，Broker 与
调用方都用它校验参数与结果——这也是"不要用 ``map<string, string>`` 当通用参数"的
落地点：参数是能力自己的结构化 Schema。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lumi_contracts.plugins.manifest import CAPABILITY_NAME_RE
from lumi_contracts.plugins.vocabulary import (
    DataLocality,
    ExecutionPlane,
    IsolationLevel,
    RuntimeKind,
    SideEffectKind,
    TrustLevel,
    executor_type_for,
    parse_data_locality,
    parse_execution_plane,
    parse_runtime_kind,
)


class CapabilityDescriptor(BaseModel):
    """能力描述符（Provider 注册时提交，Broker 据此路由与校验）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    contract_version: int = Field(default=1, ge=1)
    summary: str = ""
    #: 结构化输入/输出 Schema。缺省是"开放对象"；给了就强制校验。
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    side_effects: list[SideEffectKind] = Field(default_factory=list)
    #: 未知/缺省按最保守的 ``local_only``（本地数据不允许被无声明地送到云端）。
    data_locality: DataLocality = DataLocality.LOCAL_ONLY
    #: **执行位置**：这个能力声明在哪一侧执行（server/client）。与隔离方式正交。
    execution_plane: ExecutionPlane | None = None
    #: **运行方式**：进程内 / Worker / 容器 / 沙箱。
    runtime_kind: RuntimeKind | None = None
    required_permissions: list[str] = Field(default_factory=list)
    #: 客户端本机是否必须再确认一次（服务端授权不能替用户扩大本机权限）。
    needs_local_confirmation: bool = False
    #: 是否支持流式（``stream_cursor``）。
    streamable: bool = False
    #: 结果敏感度（"public" / "internal" / "private" / ""），由能力自述、投影使用。
    sensitivity: str = ""
    #: 稳定错误码白名单（能力声明的失败方式，便于前端给出可行动提示）。
    error_codes: list[str] = Field(default_factory=list)
    #: 大结果是否必须转 Artifact（超预算内容不进模型上下文）。
    artifact_when_large: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        text = str(value or "").strip()
        base = text.split("@", 1)[0]
        if not CAPABILITY_NAME_RE.match(base):
            raise ValueError(f"非法能力名：{text!r}（期望 形如 workspace.read，版本单独声明）")
        return base

    @field_validator("data_locality", mode="before")
    @classmethod
    def _parse_locality(cls, value: Any) -> Any:
        return parse_data_locality(value)

    @field_validator("execution_plane", mode="before")
    @classmethod
    def _parse_plane(cls, value: Any) -> Any:
        return None if value in (None, "") else parse_execution_plane(value)

    @field_validator("runtime_kind", mode="before")
    @classmethod
    def _parse_runtime(cls, value: Any) -> Any:
        return None if value in (None, "") else parse_runtime_kind(value)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _valid_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        schema = dict(value or {})
        if "type" not in schema:
            raise ValueError("JSON Schema 必须声明 type（不接受无类型参数）")
        return schema

    @model_validator(mode="after")
    def _default_isolation(self) -> "CapabilityDescriptor":
        if not self.sensitivity:
            # 本地性决定默认敏感度：只在本地跑的能力默认私有。
            self.sensitivity = (
                "private" if self.data_locality is DataLocality.LOCAL_ONLY else "internal"
            )
        # 未显式声明时按数据本地性推导（local_only→client、cloud→server），
        # 保证 executoin_plane 永远有值；hybrid 默认留在本地（数据不出本机）。
        if self.execution_plane is None:
            self.execution_plane = (
                ExecutionPlane.CLIENT
                if self.data_locality is not DataLocality.CLOUD
                else ExecutionPlane.SERVER
            )
        if self.runtime_kind is None:
            self.runtime_kind = RuntimeKind.IN_PROCESS
        return self

    @property
    def qualified_name(self) -> str:
        """``workspace.read@1``——寻址与去重的唯一键。"""
        return f"{self.name}@{self.contract_version}"

    def allows_deployment(self, deployment: Any, *, policy_allows_switch: bool = False) -> bool:
        from lumi_contracts.plugins.vocabulary import deployment_allows

        return deployment_allows(
            self.data_locality, deployment, policy_allows_switch=policy_allows_switch
        )

    def allows_registration(self, deployment: Any) -> bool:
        """**注册期**校验：这个位置是否允许声明该能力。

        与 :meth:`allows_deployment`（**调用期**路由判定）的区别在 hybrid：
        两侧都可以注册（两侧都可能有实现），但调用期要不要切到服务端仍需策略放行。
        ``local_only``/``cloud`` 的硬约束两期一致。
        """
        from lumi_contracts.plugins.vocabulary import Deployment, deployment_allows

        if self.data_locality is DataLocality.HYBRID:
            site = str(getattr(deployment, "value", deployment) or "").strip().casefold()
            return site in {item.value for item in Deployment}
        return deployment_allows(self.data_locality, deployment)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.qualified_name,
            "side_effects": [str(item) for item in self.side_effects],
            "data_locality": str(self.data_locality),
            "execution_plane": str(self.execution_plane or ExecutionPlane.CLIENT),
            "runtime_kind": str(self.runtime_kind or RuntimeKind.IN_PROCESS),
            # 兼容旧字段：前端既有读法（``provider.executor_type``）仍能拿到值。
            "executor_type": executor_type_for(
                self.execution_plane or ExecutionPlane.CLIENT,
                self.runtime_kind or RuntimeKind.IN_PROCESS,
            ),
            "sensitivity": self.sensitivity,
            "needs_local_confirmation": self.needs_local_confirmation,
            "streamable": self.streamable,
        }


def isolation_floor(trust: TrustLevel | str) -> IsolationLevel:
    """信任级别对应的隔离下限（转发到 vocabulary，保持单一判定处）。"""
    from lumi_contracts.plugins.vocabulary import isolation_for_trust

    return isolation_for_trust(trust)


__all__ = [
    "CapabilityDescriptor",
    "isolation_floor",
]
