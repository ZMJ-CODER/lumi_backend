"""阶段 4/3：步骤级**执行前**能力门禁（缺能力不拖到中途才失败）。

方案第二节 8 要求"缺能力在执行前返回 ``CAPABILITY_MISSING``，不要执行到中途才失败"。
但"缺能力"有性质完全不同的两种，混在一起做必然误伤：

* **静态不可能**（能力未声明、契约版本不符、注册位置违反数据本地性、抽象能力没映射）
  —— 这类**在执行前就阻断**，因为无论客户端是否在线它都不会成功；
* **运行时不可用**（声明了本地能力但没有 Provider＝客户端离线、租约过期）
  —— 这类**不在调度期阻断**：客户端可能几秒后就连上，而失败信息在 Broker 调用点更
  准确（带 `PROVIDER_OFFLINE`/`LEASE_EXPIRED` 与"重连"提示）。调度期只记录
  ``degraded`` 供前端提示。

这样既满足"执行前失败"，又不会因为一次离线把计划整体打死——这正是本模块只做
**保守子集**的原因。

P3 第二批把**纯决策**迁到了 backend-neutral 的 :mod:`lumi_capability.gating`：
门禁数据结构、``declared_capabilities``、``resolve_declared``、
``evaluate_node_capabilities``、``node_capability_failure``。

本模块保留的是**应用侧注入**：能力目录（``capability_catalog``）与应用自己的
"抽象能力 → 具体能力"词表（``ABSTRACT_CAPABILITY_MAP``）。公开名与签名全部不变，
既有调用点（``from ...policy.gate import node_capability_failure``）无需改动。
"""

from __future__ import annotations

from typing import Any

from lumi_contracts.plugins import (
    CapabilityResult,
    Deployment,
)

from app.agents.capabilities.catalog.legacy import CapabilityCatalog, capability_catalog
from app.agents.capabilities.registry.resolver import (
    ABSTRACT_CAPABILITY_MAP,
    CapabilityResolver,
)
from lumi_capability.gating import (
    CAPABILITY_ABSTRACT_UNMAPPED,
    CAPABILITY_DEPENDENCY_MISSING,
    CAPABILITY_LOCATION_UNSATISFIABLE,
    NodeCapabilityGate,
    NodeCapabilityIssue,
    declared_capabilities,
)
from lumi_capability.gating import evaluate_node_capabilities as _pkg_evaluate
from lumi_capability.gating import node_capability_failure as _pkg_node_failure
from lumi_capability.gating import resolve_declared as _pkg_resolve_declared


def resolve_declared(entries: list[str]) -> tuple[list[str], list[NodeCapabilityIssue]]:
    """把声明里的**抽象能力**翻成具体能力；未映射的抽象能力记为阻断问题。

    纯核在 :func:`lumi_capability.gating.resolve_declared`；本函数注入应用侧的
    抽象能力词表（``ABSTRACT_CAPABILITY_MAP``）并保留原签名。
    """
    return _pkg_resolve_declared(entries, abstract_map=ABSTRACT_CAPABILITY_MAP)


def evaluate_node_capabilities(
    node: Any,
    *,
    catalog: CapabilityCatalog | None = None,
    resolver: CapabilityResolver | None = None,
    deployment: Deployment | None = None,
    binding: Any = None,
) -> NodeCapabilityGate:
    """对单个节点做**保守**能力门禁。

    ``deployment`` 给定时还会校验（静态的）位置可行性：本地能力不能出现在服务端节点
    的执行位置上。``resolver`` 给定时额外记录运行时可用性（degraded），但不阻断。
    """
    return _pkg_evaluate(
        node,
        catalog=catalog or capability_catalog,
        abstract_map=ABSTRACT_CAPABILITY_MAP,
        resolver=resolver,
        deployment=deployment,
        binding=binding,
    )


def node_capability_failure(
    node: Any,
    *,
    catalog: CapabilityCatalog | None = None,
    resolver: CapabilityResolver | None = None,
    deployment: Deployment | None = None,
    binding: Any = None,
) -> CapabilityResult | None:
    """执行前门禁：返回结构化失败（``None`` 表示可以继续执行）。

    失败码与 Broker 的 ``CAPABILITY_MISSING`` 家族保持一致语义，前端可统一处理。
    """
    return _pkg_node_failure(
        node,
        catalog=catalog or capability_catalog,
        abstract_map=ABSTRACT_CAPABILITY_MAP,
        resolver=resolver,
        deployment=deployment,
        binding=binding,
    )


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
