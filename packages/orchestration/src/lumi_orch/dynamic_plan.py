"""计划骨架在运行时按版本扩展的契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from lumi_orch.job_spec import NodeSpec


class ExpansionSlot(BaseModel):
    """A bounded location where the scheduler may append a validated subgraph."""

    id: str = Field(min_length=1, max_length=160)
    depends_on: tuple[str, ...] = ()
    max_nodes: int = Field(default=8, ge=1, le=64)
    allowed_agents: tuple[str, ...] = ()
    allow_effects: bool = False
    status: Literal["pending", "expanded"] = "pending"


class PlanPatch(BaseModel):
    """One idempotent, version-bound expansion for a skeleton slot."""

    patch_id: str = Field(min_length=1, max_length=160)
    slot_id: str = Field(min_length=1, max_length=160)
    base_revision: int = Field(ge=1)
    source: Literal["langgraph", "external"]
    nodes: tuple[NodeSpec, ...] = ()
    slots: tuple[ExpansionSlot, ...] = ()

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "PlanPatch":
        node_ids = [node.id for node in self.nodes]
        slot_ids = [slot.id for slot in self.slots]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("PlanPatch 节点 id 重复")
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("PlanPatch 插槽 id 重复")
        if set(node_ids) & set(slot_ids):
            raise ValueError("PlanPatch 节点与插槽不能共用 id")
        return self


class PlanPatchConflict(ValueError):
    """The patch cannot be safely attached to the current plan revision."""
