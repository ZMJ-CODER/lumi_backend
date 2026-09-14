"""步骤级执行前门禁的纯函数测试（目录与抽象词表由调用方注入）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.plugins import CapabilityErrorCode

from lumi_capability import gating as g


# ── 假目录 / 假描述符（duck-typed，与真实 CapabilityCatalog 同形状）──


@dataclass
class _Descriptor:
    name: str
    contract_version: int = 1
    data_locality: str = "server"
    allowed: bool = True

    @property
    def qualified_name(self) -> str:
        return f"{self.name}@{self.contract_version}"

    def allows_deployment(self, deployment: Any) -> bool:
        return self.allowed


class _Catalog:
    def __init__(self, descriptors: list[_Descriptor]) -> None:
        self._items = {(item.name, item.contract_version): item for item in descriptors}

    def get(self, name: str, *, version: int | None = None):
        if version is not None:
            return self._items.get((name, version))
        candidates = [item for (item_name, _), item in self._items.items() if item_name == name]
        return max(candidates, key=lambda item: item.contract_version) if candidates else None

    def names(self) -> list[str]:
        return sorted({name for name, _ in self._items})


@dataclass
class _Resolution:
    available: bool
    capability: str = "workspace.read"

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "available": self.available}


@dataclass
class _Report:
    resolutions: list[_Resolution] = field(default_factory=list)


class _Resolver:
    def __init__(self, available: bool) -> None:
        self._available = available

    def resolve(self, capabilities, *, binding=None) -> _Report:
        return _Report([_Resolution(self._available, cap) for cap in capabilities])


@dataclass
class _Node:
    id: str = "n1"
    params: dict[str, Any] = field(default_factory=dict)


ABSTRACT = {"DOCUMENT_READ": ("workspace.read", "knowledge.read"), "UNMAPPED": ()}


# ── 1. 声明解析 ─────────────────────────────────────────────


def test_declared_capabilities_prefers_required_capabilities():
    node = _Node(params={"required_capabilities": ["a"], "capabilities": ["b"]})
    assert g.declared_capabilities(node) == ["a"]
    assert g.declared_capabilities(_Node(params={"capabilities": ["b"]})) == ["b"]


def test_declared_capabilities_dedupes_and_ignores_junk():
    node = _Node(params={"required_capabilities": ["a", "a", "", None, "b"]})
    assert g.declared_capabilities(node) == ["a", "b"]


def test_declared_capabilities_tolerates_missing_params():
    assert g.declared_capabilities(_Node(params={})) == []
    assert g.declared_capabilities(object()) == []


def test_resolve_declared_maps_abstract_names():
    concrete, issues = g.resolve_declared(["DOCUMENT_READ", "code.execute"], abstract_map=ABSTRACT)
    assert concrete == ["workspace.read", "knowledge.read", "code.execute"]
    assert issues == []


def test_unmapped_abstract_name_is_a_hint_not_a_block():
    """未映射的抽象能力记为**提示**：它不代表不可执行，只是调度期校验不了。"""
    concrete, issues = g.resolve_declared(["UNMAPPED"], abstract_map=ABSTRACT)
    assert concrete == []
    assert len(issues) == 1
    assert issues[0].code == g.CAPABILITY_ABSTRACT_UNMAPPED
    assert issues[0].required is False


def test_non_abstract_names_pass_through_untouched():
    concrete, issues = g.resolve_declared(["workspace.write", "workspace.write"], abstract_map=ABSTRACT)
    assert concrete == ["workspace.write"] and issues == []


# ── 2. 门禁结论 ─────────────────────────────────────────────


def test_no_declaration_means_no_gate():
    gate = g.evaluate_node_capabilities(_Node(), catalog=_Catalog([]), abstract_map=ABSTRACT)
    assert gate.ok and gate.required == [] and gate.issues == []


def test_declared_but_unknown_capability_blocks():
    """静态不可能：目录里根本没有这个能力——执行前就阻断。"""
    node = _Node(params={"required_capabilities": ["workspace.read", "nope.write"]})
    gate = g.evaluate_node_capabilities(node, catalog=_Catalog([_Descriptor("workspace.read")]), abstract_map=ABSTRACT)
    assert gate.ok is False
    codes = [item.code for item in gate.blocking]
    assert g.CAPABILITY_DEPENDENCY_MISSING in codes
    assert gate.required == ["workspace.read", "nope.write"]


def test_deployment_mismatch_blocks_with_locality_details():
    descriptor = _Descriptor("workspace.write", data_locality="client", allowed=False)
    node = _Node(params={"required_capabilities": ["workspace.write"]})
    gate = g.evaluate_node_capabilities(
        node, catalog=_Catalog([descriptor]), abstract_map=ABSTRACT, deployment="server"
    )
    assert gate.ok is False
    issue = gate.blocking[0]
    assert issue.code == g.CAPABILITY_LOCATION_UNSATISFIABLE
    assert issue.details["data_locality"] == "client"
    assert "server" in issue.message


def test_runtime_unavailability_is_degraded_not_blocking():
    """运行时不可用**不**在调度期阻断：客户端可能几秒后就连上。"""
    node = _Node(params={"required_capabilities": ["workspace.read"]})
    gate = g.evaluate_node_capabilities(
        node, catalog=_Catalog([_Descriptor("workspace.read")]), abstract_map=ABSTRACT,
        resolver=_Resolver(available=False), binding=None,
    )
    assert gate.ok is True
    assert gate.degraded and gate.degraded[0]["available"] is False


def test_available_resolution_adds_no_noise():
    node = _Node(params={"required_capabilities": ["workspace.read"]})
    gate = g.evaluate_node_capabilities(
        node, catalog=_Catalog([_Descriptor("workspace.read")]), abstract_map=ABSTRACT,
        resolver=_Resolver(available=True),
    )
    assert gate.ok is True and gate.degraded == []


def test_versioned_declaration_is_resolved_by_version():
    catalog = _Catalog([_Descriptor("workspace.read", 1), _Descriptor("workspace.read", 2)])
    node = _Node(params={"required_capabilities": ["workspace.read@1"]})
    gate = g.evaluate_node_capabilities(node, catalog=catalog, abstract_map=ABSTRACT)
    assert gate.ok is True
    missing = _Node(params={"required_capabilities": ["workspace.read@9"]})
    gate = g.evaluate_node_capabilities(missing, catalog=catalog, abstract_map=ABSTRACT)
    assert gate.ok is False and gate.blocking[0].code == g.CAPABILITY_DEPENDENCY_MISSING


# ── 3. 失败结果 ─────────────────────────────────────────────


def test_failure_aligns_dependency_missing_with_broker_code():
    """与 Broker 的"缺能力"同码：前端一套处理。"""
    node = _Node(params={"required_capabilities": ["nope"]})
    result = g.node_capability_failure(node, catalog=_Catalog([]), abstract_map=ABSTRACT)
    assert result is not None
    assert result.error_code == CapabilityErrorCode.CAPABILITY_MISSING.value
    assert "issues" in (result.error.details or {})


def test_failure_returns_none_when_gate_passes():
    node = _Node(params={"required_capabilities": ["workspace.read"]})
    assert g.node_capability_failure(node, catalog=_Catalog([_Descriptor("workspace.read")]), abstract_map=ABSTRACT) is None


def test_location_failure_keeps_its_own_code():
    descriptor = _Descriptor("workspace.write", data_locality="client", allowed=False)
    node = _Node(params={"required_capabilities": ["workspace.write"]})
    result = g.node_capability_failure(
        node, catalog=_Catalog([descriptor]), abstract_map=ABSTRACT, deployment="server"
    )
    assert result is not None
    assert result.error_code == g.CAPABILITY_LOCATION_UNSATISFIABLE


def test_gate_snapshot_shape_is_stable():
    node = _Node(params={"required_capabilities": ["nope"]})
    gate = g.evaluate_node_capabilities(node, catalog=_Catalog([]), abstract_map=ABSTRACT)
    snapshot = gate.to_snapshot()
    assert set(snapshot) == {"node_id", "ok", "blocking", "degraded", "required"}
    assert snapshot["node_id"] == "n1" and snapshot["ok"] is False
