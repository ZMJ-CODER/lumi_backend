"""阶段 4：``required_capabilities`` 自动解析（**执行前**失败，不拖到中途）。

方案第二节 8 的硬要求：Skill 只声明 ``requires.capabilities``，由
``CapabilityResolver`` 把它解析成"当前能不能跑"：

    TaskProfile.required_capabilities → CapabilityResolver → CapabilityBroker

缺能力时**在执行前**返回 ``CAPABILITY_MISSING`` 与"安装/启用 Provider"的提示，
而不是跑到第 3 步才失败（那时用户已经等了很久，且任务留下了半截副作用）。

同时给出可审计的解析结果：每个能力是否可选、当前绑定了哪个 Provider/设备/契约版本，
便于前端在计划阶段就提示"这一步需要连接客户端"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityResult,
    DataLocality,
    SessionBinding,
    capability_failure,
    executor_type_for,
)

from app.agents.capabilities.catalog.legacy import CapabilityCatalog, capability_catalog
from app.plugins.dependencies import parse_requirement


#: 抽象能力（TaskProfile 词表）→ 具体能力的映射。
#:
#: ``TaskAssessor`` 只产出抽象能力（``DOCUMENT_READ`` / ``CODE_EXECUTION`` …），
#: 因为它是跨域词表；具体能力名（``workspace.read@1``）才是 Provider 的寻址键。
#: 这张表是两者之间**唯一**的翻译处，未知抽象能力一律**忽略**（不阻断执行）。
ABSTRACT_CAPABILITY_MAP: dict[str, tuple[str, ...]] = {
    "DOCUMENT_READ": ("workspace.read@1",),
    "DOCUMENT_EDIT": ("workspace.write@1",),
    "WORKSPACE_MANIPULATION": ("workspace.read@1",),
    "CODE_EXECUTION": ("code.execute@1",),
    "GIT_OPERATIONS": ("git.operations@1",),
    "ARTIFACT_CREATE": ("artifact.create@1",),
    # 既有实现仍在服务端工具链里，暂未 Provider 化：显式忽略而不是当成缺失。
    "WEB_RESEARCH": (),
    "EMAIL_SEND": (),
    "DATABASE_QUERY": (),
}

#: 翻译后**不参与**前置门禁的具体能力（服务端已有等价实现，缺失不算阻断）。
NON_BLOCKING_CAPABILITIES: frozenset[str] = frozenset()


def normalize_capability_name(value: Any) -> str:
    """能力名归一化：去掉 ``?`` 可选前缀与 ``@版本`` 后缀（**唯一实现处**）。

    为什么必须有它：目录 / 注册表 / 插件清单里存的是**无版本基名**
    （``workspace.read``），而画像解析出来的具体能力名带契约版本
    （``workspace.read@1``，见 :data:`ABSTRACT_CAPABILITY_MAP`）。任何"按名字查集合"
    的地方（可用性判定、插件启用状态、白名单比对）都必须先过这一步，否则同一个能力
    会因写法不同被判成两个结果——曾出现"``workspace.read@1`` 被判成没有提供方"，
    把只读任务误阻断。

    只剥掉真正的数字版本号：``@beta`` 这类非数字后缀原样保留，避免误伤。
    """
    text = str(value or "").strip().lstrip("?")
    if not text:
        return ""
    name, _, version = text.partition("@")
    if version.strip().isdigit():
        return name.strip()
    return text


#: 能力名的**契约版本**后缀写法（``workspace.read@1``）；``parse_requirement`` 只认
#: ``>=`` 语义，因此这里单独解析 ``@N``（方向与 :func:`normalize_capability_name` 相反）。
def split_capability_version(value: str) -> tuple[str, str]:
    """``workspace.read@1`` → ``("workspace.read", "1")``；无版本返回 ``(name, "")``。"""
    text = str(value or "").strip().lstrip("?")
    name, _, version = text.partition("@")
    return name.strip(), version.strip()


def concrete_capabilities(abstract: list[str] | tuple[str, ...] | None) -> list[str]:
    """抽象能力 → 具体能力（去重、保序；未知抽象能力忽略）。"""
    rows: list[str] = []
    for entry in abstract or ():
        text = str(entry or "").strip().upper()
        for concrete in ABSTRACT_CAPABILITY_MAP.get(text, ()):
            if concrete not in rows:
                rows.append(concrete)
    return rows


@dataclass(slots=True)
class CapabilityResolution:
    """单个能力的解析结论。"""

    capability: str
    available: bool
    optional: bool = False
    provider_id: str = ""
    deployment: str = ""
    #: 选中 Provider 的实际执行位置与运行方式（计划门禁/前端提示都用它）。
    execution_plane: str = ""
    runtime_kind: str = ""
    device_id: str = ""
    contract_version: int = 1
    data_locality: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "available": self.available,
            "optional": self.optional,
            "provider_id": self.provider_id,
            "deployment": self.deployment,
            "execution_plane": self.execution_plane,
            "runtime_kind": self.runtime_kind,
            "executor_type": (
                executor_type_for(self.execution_plane, self.runtime_kind or "in_process")
                if self.execution_plane
                else ""
            ),
            "device_id": self.device_id,
            "contract_version": self.contract_version,
            "data_locality": self.data_locality,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RequiredCapabilitiesReport:
    """一次任务的必需能力解析报告。"""

    resolutions: list[CapabilityResolution] = field(default_factory=list)

    @property
    def missing(self) -> list[CapabilityResolution]:
        """必需但当前不可用的能力（**阻断执行**）。"""
        return [
            item
            for item in self.resolutions
            if not item.available
            and not item.optional
            and item.capability not in NON_BLOCKING_CAPABILITIES
        ]

    @property
    def degraded(self) -> list[CapabilityResolution]:
        """可选但当前不可用的能力（记录，不阻断）。"""
        return [item for item in self.resolutions if not item.available and item.optional]

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def needs_client(self) -> bool:
        """是否有必需能力必须由客户端提供（前端据此提示"请连接设备"）。"""
        return any(
            item.available and item.deployment == "client" for item in self.resolutions
        )

    def error(self) -> CapabilityResult | None:
        """缺能力时的结构化失败（供执行前直接返回）。"""
        if self.ok:
            return None
        names = "、".join(item.capability for item in self.missing[:4])
        return capability_failure(
            CapabilityErrorCode.CAPABILITY_MISSING,
            f"缺少必需能力：{names}",
            capability=self.missing[0].capability,
            suggested_action="请安装或启用提供该能力的 Provider（客户端需连接并完成能力注册）",
            details={"missing": [item.to_dict() for item in self.missing]},
        )

    def to_snapshot(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.resolutions]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "needs_client": self.needs_client,
            "missing": [item.to_dict() for item in self.missing],
            "degraded": [item.to_dict() for item in self.degraded],
            "resolutions": self.to_snapshot(),
        }


class CapabilityResolver:
    """把 ``required_capabilities`` 解析成可执行结论（唯一实现处）。"""

    def __init__(
        self,
        *,
        broker: Any = None,
        catalog: CapabilityCatalog | None = None,
    ) -> None:
        self._broker = broker
        self._catalog = catalog or _catalog_from(broker) or capability_catalog

    @property
    def catalog(self) -> CapabilityCatalog:
        return self._catalog

    def resolve(
        self,
        capabilities: list[str] | tuple[str, ...],
        *,
        binding: SessionBinding | None = None,
        policy_allows_switch: bool = False,
    ) -> RequiredCapabilitiesReport:
        """逐个解析：目录声明 + 当前绑定下有没有 Provider。"""
        report = RequiredCapabilitiesReport()
        for entry in capabilities or ():
            text = str(entry).strip()
            # 可选能力用 ``?`` 前缀声明（缺失只降级、不阻断执行）。
            optional = text.startswith("?")
            # 版本解析必须同时认 ``@1``（能力契约写法）与 ``>=``（插件依赖写法）。
            name, at_version = split_capability_version(text)
            _, op_version = parse_requirement(text.lstrip("?"))
            version_text = at_version or op_version
            descriptor = self.catalog.get(
                name, version=int(version_text) if version_text.isdigit() else None
            )
            if descriptor is None:
                report.resolutions.append(
                    CapabilityResolution(
                        capability=text,
                        available=False,
                        optional=optional,
                        reason="能力未在目录中声明",
                    )
                )
                continue
            resolution = self._resolve_one(
                descriptor,
                binding=binding,
                policy_allows_switch=policy_allows_switch,
                optional=optional,
            )
            report.resolutions.append(resolution)
        return report

    def _resolve_one(
        self,
        descriptor: CapabilityDescriptor,
        *,
        binding: SessionBinding | None,
        policy_allows_switch: bool,
        optional: bool,
    ) -> CapabilityResolution:
        base = CapabilityResolution(
            capability=descriptor.qualified_name,
            available=False,
            optional=optional,
            contract_version=descriptor.contract_version,
            data_locality=str(descriptor.data_locality),
            reason="没有可用 Provider",
        )
        if self._broker is None:
            return base
        selection = self._broker.select(
            descriptor.name,
            contract_version=descriptor.contract_version,
            binding=binding,
            policy_allows_switch=policy_allows_switch,
        )
        if not selection.provider_id:
            base.reason = selection.reason or base.reason
            return base
        base.available = True
        base.provider_id = selection.provider_id
        base.deployment = str(selection.deployment or "")
        base.execution_plane = str(selection.execution_plane or "")
        base.runtime_kind = str(selection.runtime_kind or "")
        base.device_id = str(getattr(selection.lease, "device_id", "") or "")
        base.reason = "ok"
        return base

    def is_local_only(self, capability: str) -> bool:
        descriptor = self.catalog.get(capability)
        return bool(descriptor and descriptor.data_locality is DataLocality.LOCAL_ONLY)


def _catalog_from(broker: Any) -> CapabilityCatalog | None:
    if broker is None:
        return None
    catalog = getattr(broker, "catalog", None)
    return catalog if isinstance(catalog, CapabilityCatalog) else None


__all__ = [
    "ABSTRACT_CAPABILITY_MAP",
    "CapabilityResolution",
    "CapabilityResolver",
    "NON_BLOCKING_CAPABILITIES",
    "RequiredCapabilitiesReport",
    "concrete_capabilities",
]
