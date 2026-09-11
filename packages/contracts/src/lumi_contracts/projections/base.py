"""投影契约：模型 / UI / 审计 / 持久化四类投影的统一抽象。

为什么不让业务结果类自己实现 ``render_for_model()``：那会把**业务数据**和**展示
策略**耦合在一起，导致同一个结果在模型、前端、审计三处各写一套脱敏/截断逻辑。
这里把投影抽成独立对象，由 ``ProjectionRegistry`` 统一调度。

约定：

* 每个投影拿到 ``ExecutionResult``，返回**纯 dict**（可 JSON 序列化）；
* 投影不得修改原结果（只读 + 复制）；
* 投影必须容忍未知 payload 类型：不认识就退化为保守摘要，绝不抛异常把执行结果
  弄丢（SSE/持久化路径不能因为一个投影失败而中断）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any


class ProjectionKind(StrEnum):
    MODEL = "model"
    UI = "ui"
    AUDIT = "audit"
    STORAGE = "storage"


class Projection(ABC):
    """一类输出投影。``name`` 用于注册与排障。"""

    kind: ProjectionKind

    @property
    def name(self) -> str:
        return str(self.kind.value)

    @abstractmethod
    def project(self, result: Any) -> dict[str, Any]:
        """把执行结果投影为纯 dict；实现不得抛异常。"""
        raise NotImplementedError


def payload_mapping(payload: Any) -> dict[str, Any]:
    """把 payload 规整成 dict 视图。

    业务 payload 可能是 pydantic 模型、实现了 ``to_dict`` 的业务对象或普通
    ``dataclass``；投影层不该因为"payload 不是 dict"就退化成空展示。四种投影
    共用这一个转换，行为保持一致。
    """
    if isinstance(payload, dict):
        return payload
    if payload is None:
        return {}
    for attr in ("to_dict", "model_dump", "dict"):
        method = getattr(payload, attr, None)
        if not callable(method):
            continue
        try:
            value = method()
        except Exception:  # noqa: BLE001 - 某一种转换失败就试下一种
            continue
        if isinstance(value, dict):
            return value
    if hasattr(payload, "__dict__"):
        value = {key: item for key, item in vars(payload).items() if not key.startswith("_")}
        if value:
            return value
    return {}


class _SafeProjection(Projection):
    """带兜底的投影基类：任何异常都收敛为保守摘要。"""

    def project(self, result: Any) -> dict[str, Any]:
        try:
            return self._project(result)
        except Exception as exc:  # noqa: BLE001 - 投影失败不能影响执行结果交付
            return {
                "kind": str(self.kind.value),
                "degraded": True,
                "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            }

    @abstractmethod
    def _project(self, result: Any) -> dict[str, Any]:
        raise NotImplementedError


class ProjectionRegistry:
    """投影注册表：按类型注册，按 kind 取用，未知类型退化到默认投影。"""

    def __init__(self) -> None:
        self._by_kind: dict[ProjectionKind, Projection] = {}
        self._overrides: dict[tuple[ProjectionKind, str], Projection] = {}

    # ── 注册 ──────────────────────────────────────────────

    def register(self, projection: Projection, *, replace: bool = True) -> None:
        kind = ProjectionKind(str(projection.kind))
        if kind in self._by_kind and not replace:
            raise ValueError(f"投影 {kind} 已注册")
        self._by_kind[kind] = projection

    def register_for(self, kind: ProjectionKind | str, type_name: str, projection: Projection) -> None:
        """给特定 payload 类型名注册专用投影（例如 WorkspaceNavigatorResult）。"""
        self._overrides[(ProjectionKind(str(kind)), str(type_name))] = projection

    def get(self, kind: ProjectionKind | str) -> Projection | None:
        return self._by_kind.get(ProjectionKind(str(kind)))

    def kinds(self) -> list[str]:
        return sorted(item.value for item in self._by_kind)

    # ── 投影 ──────────────────────────────────────────────

    @staticmethod
    def _type_name(result: Any) -> str:
        payload = getattr(result, "payload", None)
        if payload is not None:
            return type(payload).__name__
        if isinstance(result, dict):
            return str(result.get("schema_name") or "")
        return type(result).__name__

    def project(self, kind: ProjectionKind | str, result: Any) -> dict[str, Any]:
        """投影单个结果；未注册该 kind 时返回保守空投影（不抛异常）。"""
        normalized = ProjectionKind(str(kind))
        type_name = self._type_name(result)
        projection = self._overrides.get((normalized, type_name)) or self._by_kind.get(normalized)
        if projection is None:
            return {"kind": normalized.value, "unregistered": True, "type": type_name}
        return projection.project(result)

    def project_all(self, result: Any) -> dict[str, dict[str, Any]]:
        """一次性产出全部已注册投影（模型/UI/审计/持久化）。"""
        return {str(kind.value): self.project(kind, result) for kind in self._by_kind}


__all__ = ["Projection", "ProjectionKind", "ProjectionRegistry", "_SafeProjection", "payload_mapping"]
