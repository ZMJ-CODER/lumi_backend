"""骨架插槽与运行期补图的调度契约测试。"""

from __future__ import annotations

import asyncio

import pytest

from lumi_orch import ExpansionSlot, NodeSpec, PlanPatch, PlanPatchConflict, ResourceClaim

from app.agents.orchestration.planning.logical_plan import (
    commit_frontier_results,
    create_logical_plan,
    load_logical_plan,
    materialize_frontier,
    save_logical_plan,
)
from app.agents.orchestration.planning.logical_plan_service import LogicalPlanContinuationService
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.scheduling.service import PlanPatchScheduler
from app.agents.orchestration.runtime.state import InMemoryStateStore


def _root() -> TaskNode:
    return TaskNode(id="root", agent="test_worker", params={"instruction": "collect"})


async def _waiting_job(store: InMemoryStateStore, *, runtime: str = "legacy") -> tuple[Job, dict]:
    user_id = "patch-user"
    plan = await create_logical_plan(
        user_id,
        [_root()],
        slots=[ExpansionSlot(id="next", depends_on=("root",), allowed_agents=("test_worker",))],
    )
    plan["nodes"]["root"]["status"] = "completed"
    await save_logical_plan(user_id, plan)
    job = Job(
        job_id=f"patch-job-{runtime}",
        user_id=user_id,
        request="继续处理",
        status=JobStatus.PAUSED,
        routing={
            "runtime": runtime,
            "logical_plan": {"plan_id": plan["plan_id"], "revision": 1},
            "scheduler_waiting_slots": ["next"],
        },
    )
    await store.create_job(job)
    return job, plan


def _patch(*, revision: int = 1, node_id: str = "follow-up") -> PlanPatch:
    return PlanPatch(
        patch_id="patch-1",
        slot_id="next",
        base_revision=revision,
        source="external",
        nodes=(NodeSpec(id=node_id, agent="test_worker", params={"instruction": "follow"}),),
    )


def test_patch_appends_nodes_and_is_idempotent_on_replay():
    async def scenario():
        store = InMemoryStateStore()
        job, plan = await _waiting_job(store)
        scheduler = PlanPatchScheduler(store=store, workers={"test_worker": object()})

        first = await scheduler.append_external(job_id=job.job_id, user_id=job.user_id, patch=_patch())
        assert first.replayed is False
        assert first.requires_legacy_resume is True
        persisted = await load_logical_plan(job.user_id, plan["plan_id"])
        assert persisted is not None
        assert persisted["revision"] == 2
        assert persisted["slots"]["next"]["status"] == "expanded"
        assert persisted["nodes"]["follow-up"]["node"]["depends_on"] == ["root"]

        replay = await scheduler.append_external(job_id=job.job_id, user_id=job.user_id, patch=_patch())
        assert replay.replayed is True
        assert (await load_logical_plan(job.user_id, plan["plan_id"]))["revision"] == 2

    asyncio.run(scenario())


def test_patch_rejects_stale_revision_and_undeclared_effects():
    async def scenario():
        store = InMemoryStateStore()
        job, _ = await _waiting_job(store)
        scheduler = PlanPatchScheduler(store=store, workers={"test_worker": object()})
        with pytest.raises(PlanPatchConflict, match="版本"):
            await scheduler.append_external(
                job_id=job.job_id,
                user_id=job.user_id,
                patch=_patch(revision=2),
            )
        unsafe = PlanPatch(
            patch_id="unsafe",
            slot_id="next",
            base_revision=1,
            source="external",
            nodes=(
                NodeSpec(
                    id="write",
                    agent="test_worker",
                    params={"instruction": "write"},
                    resource_claims=(ResourceClaim(key="doc:1", mode="write"),),
                ),
            ),
        )
        with pytest.raises(PlanPatchConflict, match="副作用"):
            await scheduler.append_external(job_id=job.job_id, user_id=job.user_id, patch=unsafe)

    asyncio.run(scenario())


def test_temporal_patch_is_persisted_before_wakeup(monkeypatch):
    async def scenario():
        store = InMemoryStateStore()
        job, _ = await _waiting_job(store, runtime="temporal_logical_read")
        calls: list[str] = []

        async def signal(job_id: str, signal_name: str) -> None:
            saved = await store.get_job(job_id)
            assert saved is not None and saved.status == JobStatus.RUNNING
            calls.append(signal_name)

        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.signal_logical_read_workflow", signal
        )
        result = await PlanPatchScheduler(
            store=store, workers={"test_worker": object()}
        ).append_external(job_id=job.job_id, user_id=job.user_id, patch=_patch())
        assert result.temporal_signaled is True
        assert calls == ["plan_patch_available"]

    asyncio.run(scenario())


def test_ready_slot_prevents_logical_plan_from_completing_early():
    async def scenario():
        store = InMemoryStateStore()
        plan = await create_logical_plan(
            "wait-user",
            [_root()],
            slots=[ExpansionSlot(id="next", depends_on=("root",), allowed_agents=("test_worker",))],
        )
        plan["nodes"]["root"]["status"] = "completed"
        await save_logical_plan("wait-user", plan)
        job = Job(
            job_id="wait-job",
            user_id="wait-user",
            request="等待下一步",
            nodes=[],
            routing={"logical_plan": {"plan_id": plan["plan_id"], "revision": 1}},
        )
        await store.create_job(job)

        assert await LogicalPlanContinuationService(store=store).continue_job(job) is False
        saved = await store.get_job(job.job_id)
        assert saved is not None
        assert saved.status == JobStatus.PAUSED
        assert saved.routing["scheduler_waiting_slots"] == ["next"]

    asyncio.run(scenario())


def test_completed_rolling_plan_restores_single_terminal_answer():
    async def scenario():
        store = InMemoryStateStore()
        plan = await create_logical_plan("answer-user", [_root()])
        frontier = materialize_frontier(plan)
        assert len(frontier) == 1
        frontier[0].status = TaskStatus.COMPLETED
        frontier[0].result = {"content": "已完成只读分析。"}
        await commit_frontier_results("answer-user", plan, frontier)
        await save_logical_plan("answer-user", plan)
        job = Job(
            job_id="answer-job",
            user_id="answer-user",
            request="返回分析结果",
            nodes=frontier,
            routing={"logical_plan": {"plan_id": plan["plan_id"], "revision": 1}},
        )
        await store.create_job(job)

        assert await LogicalPlanContinuationService(store=store).continue_job(job) is False
        saved = await store.get_job(job.job_id)
        assert saved is not None
        assert saved.status == JobStatus.COMPLETED
        assert saved.result == {"final_answer": "已完成只读分析。"}

    asyncio.run(scenario())
