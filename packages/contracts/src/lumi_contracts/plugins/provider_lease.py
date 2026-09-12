"""``ProviderLease``：Provider 的注册与存活凭证（阶段 0 冻结）。

租约是"某个用户在某个设备上、某个工作区里，此刻确实有一个 Provider 能提供某个
能力"的**唯一证据**。Broker 只认租约，不认"配置里写了这个 Provider"：

* **绑定维度**：``user_id`` / ``conversation_id`` / ``workspace_id`` / ``device_id`` /
  ``session_id`` —— 不能只按用户 ID 找 Provider（同一用户多设备多工作区会越权）；
* **过期即摘除**：``expires_at`` 由心跳续期；过期租约不再参与路由，调用方收到
  ``LEASE_EXPIRED`` 而不是"工具失败"；
* **健康与能力版本分离**：``health_status`` 描述进程活着，``contract_version`` 描述
  能力契约版本；两者都可能让一次调用失败，但提示与处置完全不同。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lumi_contracts.plugins.capability_invocation import SessionBinding
from lumi_contracts.plugins.manifest import CAPABILITY_NAME_RE
from lumi_contracts.plugins.vocabulary import (
    Deployment,
    ExecutionPlane,
    ProviderHealth,
    RuntimeKind,
    TrustLevel,
    executor_type_for,
    parse_execution_plane,
    parse_runtime_kind,
    plane_for_deployment,
)


class ProviderLease(BaseModel):
    """一条能力租约（``POST /capabilities/register`` 建立，心跳续期）。"""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    capability: str
    contract_version: int = Field(default=1, ge=1)

    #: 租约主键：由 ``(provider_id, capability)`` 稳定派生（同一 Provider 的同一能力
    #: 只有一条活租约，重注册即覆盖）。派发时随调用一起透传给客户端，便于两端对账。
    lease_id: str = ""

    # ── 绑定 ──
    user_id: str = ""
    device_id: str = ""
    conversation_id: str = ""
    workspace_id: str = ""
    session_id: str = ""

    # ── 运行位置与信任 ──
    deployment: Deployment = Deployment.CLIENT
    #: **本次租约实际绑定的执行位置**与运行方式（Provider 注册/心跳时上报；
    #: 缺省按 ``deployment`` 推导，保证租约永远能回答"谁在执行"）。
    execution_plane: ExecutionPlane | None = None
    runtime_kind: RuntimeKind | None = None
    trust_level: TrustLevel = TrustLevel.THIRD_PARTY
    plugin_id: str = ""
    plugin_version: str = ""
    #: Provider 自述的实现版本（用于审计"当时用的是哪个实现"）。
    provider_version: str = ""

    # ── 作用域与健康 ──
    scope: dict[str, Any] = Field(default_factory=dict)
    health_status: ProviderHealth = ProviderHealth.UNKNOWN

    # ── 时间（epoch 秒，便于跨进程比较）──
    issued_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0
    last_heartbeat_at: float = Field(default_factory=time.time)

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        text = str(value or "").strip()
        base = text.split("@", 1)[0]
        if not CAPABILITY_NAME_RE.match(base):
            raise ValueError(f"非法能力名：{text!r}")
        return base

    @field_validator("provider_id")
    @classmethod
    def _valid_provider(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("provider_id 不能为空")
        return text[:160]

    @field_validator("execution_plane", mode="before")
    @classmethod
    def _parse_plane(cls, value: Any) -> Any:
        return None if value in (None, "") else parse_execution_plane(value)

    @field_validator("runtime_kind", mode="before")
    @classmethod
    def _parse_runtime(cls, value: Any) -> Any:
        return None if value in (None, "") else parse_runtime_kind(value)

    def plane(self) -> ExecutionPlane:
        """生效执行位置（显式声明优先，否则由 ``deployment`` 推导）。

        用解析函数兜底：``model_copy`` 之类的路径可能把字符串塞进来（不做校验），
        访问器必须保证返回枚举，否则调用方 ``is ExecutionPlane.CLIENT`` 会静默失败。
        """
        if self.execution_plane is None or self.execution_plane == "":
            return plane_for_deployment(self.deployment)
        return parse_execution_plane(self.execution_plane)

    def runtime(self) -> RuntimeKind:
        """生效运行方式（显式声明优先，否则进程内）。"""
        if self.runtime_kind is None or self.runtime_kind == "":
            return RuntimeKind.IN_PROCESS
        return parse_runtime_kind(self.runtime_kind)

    def executor_type(self) -> str:
        """兼容旧字段的派生值（``server``/``client``/``worker``/``container``/``sandbox``）。"""
        return executor_type_for(self.plane(), self.runtime())

    def is_expired(self, *, now: float | None = None) -> bool:
        """过期判定；``expires_at<=0`` 视为**未设租约**（过期）。"""
        if self.expires_at <= 0:
            return True
        return (time.time() if now is None else float(now)) >= self.expires_at

    def is_usable(self, *, now: float | None = None) -> bool:
        """可用 = 未过期 且 健康（``UNKNOWN`` 视为可用：刚注册还没体检）。"""
        if self.is_expired(now=now):
            return False
        return self.health_status in {ProviderHealth.HEALTHY, ProviderHealth.UNKNOWN}

    @property
    def qualified_capability(self) -> str:
        return f"{self.capability}@{self.contract_version}"

    def binding(self) -> SessionBinding:
        return SessionBinding(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            workspace_id=self.workspace_id,
            device_id=self.device_id,
            session_id=self.session_id,
        )

    def matches(self, binding: SessionBinding, *, strict_workspace: bool = True) -> bool:
        """租约是否覆盖该绑定。

        规则：租约里为空的维度表示"该用户/设备上通用"；租约里填了的维度必须与请求
        完全一致。``strict_workspace=True``（默认）时工作区必须完全相等——绝不把
        "任意工作区"的 Provider 当成"这个工作区"的 Provider（那是越权读文件）。
        """
        if str(binding.user_id or "") and str(self.user_id or "") != str(binding.user_id):
            return False
        if str(binding.device_id or "") and str(self.device_id or "") != str(binding.device_id):
            return False
        for requested, bound in (
            (binding.conversation_id, self.conversation_id),
            (binding.session_id, self.session_id),
        ):
            if str(requested or "") and str(bound or "") not in {"", str(requested)}:
                return False
        if str(binding.workspace_id or ""):
            if strict_workspace:
                if str(self.workspace_id or "") != str(binding.workspace_id):
                    return False
            elif str(self.workspace_id or "") not in {"", str(binding.workspace_id)}:
                return False
        return True

    def renew(self, *, ttl_seconds: float, now: float | None = None) -> "ProviderLease":
        """心跳续期（原地返回新对象；``ttl_seconds<=0`` 视为不续期）。"""
        stamp = time.time() if now is None else float(now)
        ttl = max(0.0, float(ttl_seconds))
        return self.model_copy(
            update={
                "last_heartbeat_at": stamp,
                "expires_at": (stamp + ttl) if ttl > 0 else self.expires_at,
            }
        )

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id or lease_id_hint(self.provider_id, self.capability),
            "provider_id": self.provider_id,
            "capability": self.qualified_capability,
            "deployment": str(self.deployment),
            # 本次租约实际绑定的位置与运行方式（不是"配置里写了什么"）。
            "execution_plane": str(self.plane()),
            "runtime_kind": str(self.runtime()),
            "executor_type": self.executor_type(),
            "device_id": self.device_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "provider_version": self.provider_version,
            "health_status": str(self.health_status),
            "expires_at": self.expires_at,
        }


def lease_id_hint(provider_id: str, capability: str) -> str:
    """租约主键的稳定摘要（与 ``app.services.capability_lease_redis.lease_id_for`` 同源）。

    放在契约层是因为两侧都要用同一算法：客户端回传 ``lease_id``、服务端校验时必须一致。
    """
    import hashlib

    raw = f"{str(provider_id or '')}|{str(capability or '').split('@', 1)[0]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


__all__ = ["ProviderLease", "lease_id_hint"]
