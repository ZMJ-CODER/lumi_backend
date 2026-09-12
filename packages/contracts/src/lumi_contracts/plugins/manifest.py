"""``PluginManifest``：插件自述（阶段 0 冻结）。

Manifest 是**安装与激活的唯一事实来源**：它声明这是什么插件、装到哪一侧、需要什么
权限、有没有副作用、怎么健康检查、由谁签名。三条设计约束：

1. **结构化字段，不用 ``map<string, string>``**：能力参数、权限、资源上限都是各自
   有 Schema 的结构，避免"万能字符串字典"把类型错误推迟到运行时；
2. **保守默认**：缺字段时按最严格的一侧取值（位置默认服务端、本地性默认
   ``local_only``、信任默认第三方、副作用默认有写），绝不放行未声明的能力；
3. **自述不算授权**：``trust_level`` 与 ``signature`` 只是声明，真正的信任级别由
   安装期的验签结果覆盖（见 ``PluginInstaller``），Manifest 说自己是官方不算数。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lumi_contracts.plugins.vocabulary import (
    DataLocality,
    Deployment,
    ExecutionPlane,
    IsolationLevel,
    PluginKind,
    RuntimeKind,
    SideEffectKind,
    TrustLevel,
    executor_type_for,
    isolation_for_trust,
    parse_data_locality,
    parse_execution_plane,
    parse_plugin_kind,
    parse_runtime_kind,
    plane_for_deployment,
    runtime_kind_for_isolation,
)

# 插件 id 采用反向域名风格：小写字母/数字/点/横线/下划线，必须以字母开头。
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,159}$")
# 能力名形如 ``workspace.read``（小写点分），版本用 ``@<n>`` 单独表达。
CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class PluginPermission(BaseModel):
    """一条权限声明：能做什么 + 作用域 + 是否需要本机确认。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    scope: str = ""
    required: bool = True
    #: 客户端本地拒止策略也会校验；服务端授权不能替用户扩大本机权限。
    needs_local_confirmation: bool = False
    reason: str = ""


class PluginRequires(BaseModel):
    """依赖声明：能力、其他插件、最低 Lumi 版本、策略包。"""

    model_config = ConfigDict(extra="forbid")

    #: 形如 ``workspace.read@1``。Skill 不知道能力跑在哪一侧，只声明需要什么。
    capabilities: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    min_lumi_version: str = ""
    policies: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "plugins", "policies")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class PluginProvides(BaseModel):
    """产出声明：能力、策略、视图、技能入口。"""

    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    views: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "policies", "views", "skills")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned


class PluginEntrypoints(BaseModel):
    """入口声明：按插件类型给出可执行入口（服务端/Worker 侧才是代码入口）。

    * ``module`` + ``callable``：服务端/Worker 插件的 Python 入口。第三方插件不在
      API 进程内 ``importlib`` 加载，实际加载由隔离层（Worker/容器）负责；
    * ``capability``：``capability_provider`` 声明它提供哪个能力；
    * ``view_type``：``view_plugin`` 的声明式视图类型（不允许注入 JSX/JS）；
    * ``policy_id``：``policy_pack`` 的策略集合。
    """

    model_config = ConfigDict(extra="forbid")

    module: str = ""
    callable: str = ""
    capability: str = ""
    view_type: str = ""
    policy_id: str = ""
    #: HTTP/stdio 入口（客户端 Provider 用），不是任意可执行路径。
    transport: str = ""


class PluginResourceLimits(BaseModel):
    """资源上限（服务端与客户端都据此拒绝，而不是"尽力而为"）。"""

    model_config = ConfigDict(extra="forbid")

    memory_mb: int = Field(default=256, ge=1, le=65536)
    cpu_seconds: float = Field(default=30.0, gt=0, le=3600.0)
    wall_seconds: float = Field(default=60.0, gt=0, le=86400.0)
    max_output_bytes: int = Field(default=2_000_000, ge=1024, le=200_000_000)
    max_concurrency: int = Field(default=1, ge=1, le=64)


class PluginHealthcheck(BaseModel):
    """健康检查声明：怎么判断插件还活着（安装期 + 运行期共用）。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = "none"          # none / import / http / stdio
    target: str = ""
    interval_seconds: float = Field(default=30.0, gt=0, le=3600.0)
    timeout_seconds: float = Field(default=5.0, gt=0, le=300.0)
    failure_threshold: int = Field(default=3, ge=1, le=100)


class PluginSignature(BaseModel):
    """签名声明与验签结果（``verified`` 只能由安装器写入）。"""

    model_config = ConfigDict(extra="forbid")

    algorithm: str = ""
    key_id: str = ""
    digest: str = ""
    value: str = ""
    signed_at: float = 0.0
    #: 验签结果：Manifest 自述无法把自己变成"已验证"。
    verified: bool = False
    verified_at: float = 0.0
    signer: str = ""


class PluginManifest(BaseModel):
    """插件自述契约。``schema_version`` 是契约版本（不是插件版本）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    id: str
    version: str
    kind: PluginKind
    deployment: Deployment = Deployment.SERVER

    #: 面向用户的展示名/说明（可空，不参与判定）。
    name: str = ""
    description: str = ""

    requires: PluginRequires = Field(default_factory=PluginRequires)
    provides: PluginProvides = Field(default_factory=PluginProvides)
    entrypoints: PluginEntrypoints = Field(default_factory=PluginEntrypoints)
    permissions: list[PluginPermission] = Field(default_factory=list)

    #: 数据本地性：未知值按最保守的 ``local_only``（见 vocabulary）。
    data_locality: DataLocality = DataLocality.LOCAL_ONLY
    isolation: IsolationLevel = IsolationLevel.SANDBOXED
    #: **声明**的执行位置与运行方式（不是实际执行结果——实际值在租约/结果/审计里）。
    #: 缺省由 ``deployment`` / ``isolation`` 推导，显式声明优先。
    execution_plane: ExecutionPlane | None = None
    runtime_kind: RuntimeKind | None = None
    side_effects: list[SideEffectKind] = Field(default_factory=list)
    resource_limits: PluginResourceLimits = Field(default_factory=PluginResourceLimits)
    healthcheck: PluginHealthcheck = Field(default_factory=PluginHealthcheck)
    signature: PluginSignature = Field(default_factory=PluginSignature)
    trust_level: TrustLevel = TrustLevel.THIRD_PARTY

    # ── 校验 ──────────────────────────────────────────────

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not PLUGIN_ID_RE.match(text):
            raise ValueError(f"非法插件 id：{text!r}（小写字母开头，允许 . _ -，长度 3~160）")
        return text

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        text = str(value or "").strip()
        if not SEMVER_RE.match(text):
            raise ValueError(f"非法插件版本：{text!r}（期望 semver，如 1.2.0）")
        return text

    @field_validator("kind", mode="before")
    @classmethod
    def _parse_kind(cls, value: Any) -> Any:
        parsed = parse_plugin_kind(value)
        if parsed is None:
            # 未知类型必须在**解析阶段**失败，不能落到"默认放行"。
            raise ValueError(f"未知插件类型：{value!r}（需要 Extension Handler 注册后才允许）")
        return parsed

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

    def declared_plane(self) -> ExecutionPlane:
        """Manifest **声明**的执行位置（缺省由 ``deployment`` 推导）。"""
        return self.execution_plane or plane_for_deployment(self.deployment)

    def declared_runtime(self) -> RuntimeKind:
        """Manifest **声明**的运行方式（缺省由 ``isolation`` 推导）。"""
        return self.runtime_kind or runtime_kind_for_isolation(self.isolation)

    @model_validator(mode="after")
    def _check_consistency(self) -> "PluginManifest":
        # 客户端 Provider 的代码不在服务端加载；声明 module 会被拒绝（防越权加载）。
        if self.deployment is Deployment.CLIENT and self.entrypoints.module:
            raise ValueError("客户端插件的 Manifest 不得声明服务端 module 入口")
        # 能力 Provider 必须说明它提供哪个能力。
        if self.kind is PluginKind.CAPABILITY_PROVIDER and not self.entrypoints.capability:
            raise ValueError("capability_provider 必须声明 entrypoints.capability")
        if self.kind is PluginKind.VIEW_PLUGIN and not self.entrypoints.view_type:
            raise ValueError("view_plugin 必须声明 entrypoints.view_type")
        if self.kind is PluginKind.POLICY_PACK and not self.entrypoints.policy_id:
            raise ValueError("policy_pack 必须声明 entrypoints.policy_id")
        # 隔离强度不得弱于信任级别要求（自述也不能自降隔离）。
        required = isolation_for_trust(self.trust_level)
        order = [
            IsolationLevel.IN_PROCESS,
            IsolationLevel.RESTRICTED_WORKER,
            IsolationLevel.SANDBOXED,
            IsolationLevel.CLIENT_DEVICE,
        ]
        if self.deployment is not Deployment.CLIENT and order.index(self.isolation) < order.index(required):
            raise ValueError(
                f"隔离强度不足：trust={self.trust_level} 至少需要 {required}，"
                f"Manifest 声明的是 {self.isolation}"
            )
        for name in self.provides.capabilities + self.requires.capabilities:
            base = str(name).split("@", 1)[0]
            if not CAPABILITY_NAME_RE.match(base):
                raise ValueError(f"非法能力名：{name!r}（期望 形如 workspace.read@1）")
        return self

    # ── 派生 ──────────────────────────────────────────────

    @property
    def needs_approval(self) -> bool:
        from lumi_contracts.plugins.vocabulary import APPROVAL_REQUIRED_SIDE_EFFECTS

        return any(item.value in APPROVAL_REQUIRED_SIDE_EFFECTS for item in self.side_effects)

    def digest(self) -> str:
        """Manifest 规范化摘要（安装快照/版本锁定用；不含验签结果）。"""
        payload = self.model_dump(mode="json", exclude={"signature"})
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_snapshot(self) -> dict[str, Any]:
        """落库快照（JSON-safe；只放恢复/审计需要的字段）。

        ⚠️ 这里放的是**插件声明**（declared），不是实际执行结果：实际用了哪一侧、
        什么运行方式，以租约（``ProviderLease``）/结果（``CapabilityResult``）/
        审计记录为准。
        """
        return {
            "id": self.id,
            "version": self.version,
            "kind": str(self.kind),
            "deployment": str(self.deployment),
            "trust_level": str(self.trust_level),
            "data_locality": str(self.data_locality),
            # 声明值（declared_*）：前端可据此展示"插件声称跑在哪"，与实际执行值区分。
            "execution_plane": str(self.declared_plane()),
            "runtime_kind": str(self.declared_runtime()),
            "executor_type": executor_type_for(self.declared_plane(), self.declared_runtime()),
            "declared_execution_plane": str(self.declared_plane()),
            "declared_runtime_kind": str(self.declared_runtime()),
            "digest": self.digest(),
            "provides": self.provides.model_dump(mode="json"),
            "requires": self.requires.model_dump(mode="json"),
        }


__all__ = [
    "CAPABILITY_NAME_RE",
    "PLUGIN_ID_RE",
    "PluginEntrypoints",
    "PluginHealthcheck",
    "PluginManifest",
    "PluginPermission",
    "PluginProvides",
    "PluginRequires",
    "PluginResourceLimits",
    "PluginSignature",
]
