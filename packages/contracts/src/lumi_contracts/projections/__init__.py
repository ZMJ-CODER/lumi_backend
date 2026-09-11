"""投影层：把执行结果投影为模型 / UI / 审计 / 持久化四种形状。

用法::

    from lumi_contracts.projections import default_projection_registry

    views = default_projection_registry().project_all(result)
    model_text = views["model"]["text"]
"""

from __future__ import annotations

from lumi_contracts.projections.audit import AuditProjection
from lumi_contracts.projections.base import Projection, ProjectionKind, ProjectionRegistry
from lumi_contracts.projections.model import (
    DEFAULT_ITEM_BUDGET,
    DEFAULT_MODEL_BUDGET,
    ModelProjection,
)
from lumi_contracts.projections.storage import StorageProjection
from lumi_contracts.projections.ui import UiProjection

_DEFAULT_REGISTRY: ProjectionRegistry | None = None


def default_projection_registry(*, model_budget: int = DEFAULT_MODEL_BUDGET) -> ProjectionRegistry:
    """进程内默认投影注册表（四类投影各一个）。"""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        registry = ProjectionRegistry()
        registry.register(ModelProjection(budget=model_budget))
        registry.register(UiProjection())
        registry.register(AuditProjection())
        registry.register(StorageProjection())
        _DEFAULT_REGISTRY = registry
    return _DEFAULT_REGISTRY


__all__ = [
    "AuditProjection",
    "DEFAULT_ITEM_BUDGET",
    "DEFAULT_MODEL_BUDGET",
    "ModelProjection",
    "Projection",
    "ProjectionKind",
    "ProjectionRegistry",
    "StorageProjection",
    "UiProjection",
    "default_projection_registry",
]
