"""Contract tests for LangGraph candidate fan-out before runtime plan patches."""

from __future__ import annotations

import asyncio

import pytest

from lumi_orch import NodeSpec, PlanPatchConflict

from app.agents.orchestration.scheduling.langgraph_send import (
    DEFAULT_CANDIDATE_NODE,
    build_langgraph_plan_patch,
    commit_langgraph_plan_patch,
    fan_out_patch_candidates,
)


def test_send_fan_out_only_contains_candidate_data_in_stable_order():
    sends = fan_out_patch_candidates(
        [
            NodeSpec(id="first", agent="retrieval", params={"query": "A"}),
            {"id": "second", "agent": "retrieval", "params": {"query": "B"}},
        ]
    )

    assert [send.node for send in sends] == [DEFAULT_CANDIDATE_NODE, DEFAULT_CANDIDATE_NODE]
    assert [send.arg["candidate_index"] for send in sends] == [0, 1]
    assert [send.arg["node"]["id"] for send in sends] == ["first", "second"]
    assert "job_id" not in sends[0].arg
    assert "patch_id" not in sends[0].arg


def test_joined_candidates_become_a_langgraph_only_patch():
    patch = build_langgraph_plan_patch(
        patch_id="patch-1",
        slot_id="follow-up",
        base_revision=3,
        candidates=[{"id": "next", "agent": "retrieval", "params": {"query": "资料"}}],
    )

    assert patch.source == "langgraph"
    assert patch.nodes[0].id == "next"

    with pytest.raises(PlanPatchConflict, match="候选为空"):
        build_langgraph_plan_patch(
            patch_id="empty",
            slot_id="follow-up",
            base_revision=3,
            candidates=[],
        )


def test_commit_delegates_to_the_shared_scheduler_gate():
    class FakeScheduler:
        async def append_langgraph(self, *, job_id, user_id, patch):
            return {"job_id": job_id, "user_id": user_id, "patch": patch}

    async def scenario():
        patch = build_langgraph_plan_patch(
            patch_id="patch-1",
            slot_id="follow-up",
            base_revision=1,
            candidates=[{"id": "next", "agent": "retrieval"}],
        )
        result = await commit_langgraph_plan_patch(
            scheduler=FakeScheduler(), job_id="job-1", user_id="user-1", patch=patch
        )
        assert result["job_id"] == "job-1"
        assert result["patch"].source == "langgraph"

    asyncio.run(scenario())
