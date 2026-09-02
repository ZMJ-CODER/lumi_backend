"""使用 LangGraph ``Send`` 实现受限运行时计划扩展的适配器。

``Send`` 仅用于扇出候选节点准备。它不能运行工作节点、变更逻辑计划或绕过调度
器。扇出汇合后，调用方创建一个绑定版本的 :class:`PlanPatch`，再交给
``PlanPatchScheduler.append_langgraph``。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langgraph.types import Send
from lumi_orch import ExpansionSlot, NodeSpec, PlanPatch, PlanPatchConflict

from app.agents.orchestration.scheduling.service import PlanPatchAppendResult, PlanPatchScheduler


DEFAULT_CANDIDATE_NODE = "prepare_plan_patch_candidate"


def fan_out_patch_candidates(
    nodes: Iterable[NodeSpec | dict[str, Any]],
    *,
    target: str = DEFAULT_CANDIDATE_NODE,
) -> list[Send]:
    """Map candidate nodes to a LangGraph preparation node in stable order.

    The generated payload deliberately has no job id, owner id, patch id, or
    execution capability.  A LangGraph node may enrich an individual
    candidate, but only the joined caller has enough context to submit it.
    """
    if not target.strip():
        raise ValueError("LangGraph 候选处理节点不能为空")
    sends: list[Send] = []
    for index, raw_node in enumerate(nodes):
        node = NodeSpec.model_validate(raw_node)
        sends.append(
            Send(
                target,
                {
                    "candidate_index": index,
                    "node": node.model_dump(mode="json"),
                },
            )
        )
    return sends


def build_langgraph_plan_patch(
    *,
    patch_id: str,
    slot_id: str,
    base_revision: int,
    candidates: Iterable[NodeSpec | dict[str, Any]],
    child_slots: Iterable[ExpansionSlot | dict[str, Any]] = (),
) -> PlanPatch:
    """Join prepared candidates into the only patch shape the scheduler accepts."""
    nodes = tuple(NodeSpec.model_validate(candidate) for candidate in candidates)
    slots = tuple(ExpansionSlot.model_validate(slot) for slot in child_slots)
    if not nodes and not slots:
        raise PlanPatchConflict("LangGraph 候选为空，拒绝创建空补图")
    return PlanPatch(
        patch_id=patch_id,
        slot_id=slot_id,
        base_revision=base_revision,
        source="langgraph",
        nodes=nodes,
        slots=slots,
    )


async def commit_langgraph_plan_patch(
    *,
    scheduler: PlanPatchScheduler,
    job_id: str,
    user_id: str,
    patch: PlanPatch,
) -> PlanPatchAppendResult:
    """Persist a joined LangGraph patch through the shared scheduling gate."""
    if patch.source != "langgraph":
        raise PlanPatchConflict("LangGraph 适配器只接受 source=langgraph 的补图")
    return await scheduler.append_langgraph(job_id=job_id, user_id=user_id, patch=patch)
