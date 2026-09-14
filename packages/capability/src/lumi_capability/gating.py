"""步骤级**执行前**能力门禁（纯决策）。

从 ``app/agents/capabilities/policy/gate.py`` 抽出（结构重构 P3 第二批）。
方案要求"缺能力在执行前返回 ``CAPABILITY_MISSING``，不要执行到中途才失败"，
但"缺能力"有性质完全不同的两种，混在一起必然误伤：

* **静态不可能**（能力未声明、契约版本不符、注册位置违反数据本地性、抽象能力没映射）
  —— **执行前阻断**，因为无论客户端是否在线它都不会成功；
* **运行时不可用**（声明了本地能力但没有 Provider＝客户端离线、租约过期）
  —— **不在调度期阻断**：客户端可能几秒后就连上，而失败信息在 Broker 调用点更准确。
  调度期只记录 ``degraded`` 供前端提示。

依赖注入：能力目录（``catalog``）与"抽象能力 → 具体能力"的词表（``abstract_map``）
**由调用方传入**。这样这个模块既不需要认识应用目录，也不需要认识应用的抽象词表——
纯决策与"应用里有什么"彻底分开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from lumi_contracts.plugins import (
    CapabilityErrorCode,
    CapabilityResult,
    Deployment,
    capability_failure,
)

#: 门禁的稳定错误码。
CAPABILITY_DEPENDENCY_MISSING = "CAPABILITY_DEPENDENCY_MISSING"
CAPABILITY_LOCATION_UNSATISFIABLE = "CAPABILITY_LOCATION_UNSATISFIABLE"
CAPABILITY_ABSTRACT_UNMAPPED = "CAPABILITY_ABSTRACT_UNMAPPED"


@dataclass(slots=True)
class NodeCapabilityIssue:
    capability: str
    code: str
    message: str
    required: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "code": self.code,
            "message": self.message,
            "required": self.required,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class NodeCapabilityGate:
    """一个节点的能力门禁结论（``blocking`` 非空时不得执行）。"""

    node_id: str = ""
    issues: list[NodeCapabilityIssue] = field(default_factory=list)
    #: 运行时不可用（记录 + 前端提示，不阻断调度）。
    degraded: list[dict[str, Any]] = field(default_factory=list)
    required: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[NodeCapabilityIssue]:
        return [item for item in self.issues if item.required]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def to_failure(self) -> CapabilityResult:
        head = self.blocking[0]
        return capability_failure(
            head.code,
            head.message,
            capability=head.capability,
            suggested_action="请修正节点的能力声明或安装/启用对应 Provider",
            details={"issues": [item.to_dict() for item in self.blocking]},
        )

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "ok": self.ok,
            "blocking": [item.to_dict() for item in self.blocking],
            "degraded": list(self.degraded),
            "required": list(self.required),
        }


def declared_capabilities(node: Any) -> list[str]:
    """节点声明的能力：``params["required_capabilities"]`` 优先，其次 ``params["capabilities"]``。

    只读参数，不猜工具名——"哪个工具需要哪个能力"由 Provider/目录决定，不从名字推断。
    """
    params = getattr(node, "params", None) or {}
    if not isinstance(params, dict):
        return []
    raw = params.get("required_capabilities") or params.get("capabilities") or []
    rows: list[str] = []
    for item in raw if isinstance(raw, (list, tuple, set)) else [raw]:
        text = str(item or "").strip()
        if text and text not in rows:
            rows.append(text)
    return rows


def resolve_declared(
    entries: list[str],
    *,
    abstract_map: Mapping[str, Any],
) -> tuple[list[str], list[NodeCapabilityIssue]]:
    """把声明里的**抽象能力**翻成具体能力；未映射的抽象能力记为提示问题。"""
    concrete: list[str] = []
    issues: list[NodeCapabilityIssue] = []
    for entry in entries:
        text = str(entry or "").strip()
        if text.upper() not in abstract_map:
            if text not in concrete:
                concrete.append(text)
            continue
        mapped = abstract_map.get(text.upper(), ())
        if not mapped:
            issues.append(
                NodeCapabilityIssue(
                    capability=text,
                    code=CAPABILITY_ABSTRACT_UNMAPPED,
                    message=f"抽象能力 {text} 尚未映射到具体能力（无法在调度期校验）",
                    required=False,  # 未映射不等于不可执行：记录为提示
                    details={"abstract": text},
                )
            )
            continue
        for name in mapped:
            if name not in concrete:
                concrete.append(name)
    return concrete, issues


def evaluate_node_capabilities(
    node: Any,
    *,
    catalog: Any,
    abstract_map: Mapping[str, Any],
    resolver: Any = None,
    deployment: Deployment | None = None,
    binding: Any = None,
) -> NodeCapabilityGate:
    """对单个节点做**保守**能力门禁。

    ``deployment`` 给定时还会校验（静态的）位置可行性：本地能力不能出现在服务端节点
    的执行位置上。``resolver`` 给定时额外记录运行时可用性（degraded），但不阻断。
    """
    node_id = str(getattr(node, "id", "") or "")
    entries = declared_capabilities(node)
    gate = NodeCapabilityGate(node_id=node_id)
    if not entries:
        return gate
    concrete, abstract_issues = resolve_declared(entries, abstract_map=abstract_map)
    gate.issues.extend(abstract_issues)
    gate.required = list(concrete)
    for name in concrete:
        base, _, version_text = str(name).partition("@")
        version = int(version_text) if version_text.strip().isdigit() else None
        descriptor = catalog.get(base, version=version)
        if descriptor is None:
            gate.issues.append(
                NodeCapabilityIssue(
                    capability=name,
                    code=CAPABILITY_DEPENDENCY_MISSING,
                    message=f"节点声明了未声明的能力：{name}",
                    details={"capability": name, "known": list(catalog.names())},
                )
            )
            continue
        if deployment is not None and not descriptor.allows_deployment(deployment):
            gate.issues.append(
                NodeCapabilityIssue(
                    capability=descriptor.qualified_name,
                    code=CAPABILITY_LOCATION_UNSATISFIABLE,
                    message=(
                        f"{descriptor.qualified_name}（{descriptor.data_locality}）"
                        f"不能在 {deployment} 侧执行"
                    ),
                    details={
                        "data_locality": str(descriptor.data_locality),
                        "deployment": str(deployment),
                    },
                )
            )
            continue
        if resolver is not None:
            report = resolver.resolve([descriptor.qualified_name], binding=binding)
            for resolution in report.resolutions:
                if not resolution.available:
                    # 运行时不可用：只记录，交给 Broker 在调用点给出准确错误。
                    gate.degraded.append(resolution.to_dict())
    return gate


def node_capability_failure(
    node: Any,
    *,
    catalog: Any,
    abstract_map: Mapping[str, Any],
    resolver: Any = None,
    deployment: Deployment | None = None,
    binding: Any = None,
) -> CapabilityResult | None:
    """执行前门禁：返回结构化失败（``None`` 表示可以继续执行）。

    失败码与 Broker 的 ``CAPABILITY_MISSING`` 家族保持一致语义，前端可统一处理。
    """
    gate = evaluate_node_capabilities(
        node,
        catalog=catalog,
        abstract_map=abstract_map,
        resolver=resolver,
        deployment=deployment,
        binding=binding,
    )
    if gate.ok:
        return None
    result = gate.to_failure()
    if result.error_code == CAPABILITY_DEPENDENCY_MISSING and result.error is not None:
        # 与 Broker 的"缺能力"对齐：都提示安装/启用 Provider（前端一套处理）。
        return result.model_copy(
            update={
                "error": result.error.model_copy(
                    update={"code": CapabilityErrorCode.CAPABILITY_MISSING.value}
                )
            }
        )
    return result


__all__ = [
    "CAPABILITY_ABSTRACT_UNMAPPED",
    "CAPABILITY_DEPENDENCY_MISSING",
    "CAPABILITY_LOCATION_UNSATISFIABLE",
    "NodeCapabilityGate",
    "NodeCapabilityIssue",
    "declared_capabilities",
    "evaluate_node_capabilities",
    "node_capability_failure",
    "resolve_declared",
]
