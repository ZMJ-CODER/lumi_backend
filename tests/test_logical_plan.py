"""Rolling materialization for ordinary office DAGs."""

from __future__ import annotations

import asyncio

from app.agents.orchestration.context import build_dependency_context_from_refs
from app.agents.orchestration.logical_plan import (
    commit_frontier_results,
    create_logical_plan,
    logical_plan_progress,
    materialize_frontier,
)
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planner import Planner, TaskTree
from app.agents.orchestration.state import InMemoryStateStore


def _node(node_id: str, *, depends_on: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id=node_id,
        name=node_id,
        agent="test_worker",
        params={"instruction": f"run {node_id}"},
        depends_on=depends_on or [],
    )


def test_logical_plan_materializes_only_ready_frontier_and_uses_result_refs():
    async def scenario():
        plan = await create_logical_plan(
            "logical-owner",
            [_node("read"), _node("transform", depends_on=["read"]), _node("deliver", depends_on=["transform"])],
        )
        first = materialize_frontier(plan, limit=1)
        assert [node.id for node in first] == ["read"]
        assert len(plan["nodes"]) == 3

        first[0].status = TaskStatus.COMPLETED
        first[0].result = {"success": True, "content": "source facts", "secret": "discard"}
        await commit_frontier_results("logical-owner", plan, first)
        second = materialize_frontier(plan, limit=1)

        assert [node.id for node in second] == ["transform"]
        assert second[0].depends_on == []
        ref = second[0].metadata["logical_dependency_refs"]["read"]
        assert set(ref) == {"id", "sha256"}
        assert "source facts" not in str(second[0].metadata)
        restored = await build_dependency_context_from_refs(
            second[0], {}, user_id="logical-owner"
        )
        assert restored["read"]["content"] == "source facts"
        assert "secret" not in restored["read"]

    asyncio.run(scenario())


def test_logical_plan_does_not_materialize_when_budget_is_exhausted():
    async def scenario():
        plan = await create_logical_plan("budget-owner", [_node("expensive")])
        plan["budget"]["limit"] = 1
        assert materialize_frontier(plan, limit=1) == []
        assert logical_plan_progress(plan)["pending"] == 1

    asyncio.run(scenario())


def test_logical_plan_replan_replaces_only_unfinished_tail():
    class ReplacementPlanner(Planner):
        async def plan(self, *args, **kwargs):
            raise AssertionError("replan must use the level-aware planner")

        async def plan_for_level(self, *args, **kwargs):
            return TaskTree(nodes=[_node("replacement")], plan_text="use replacement")

    async def scenario():
        store = InMemoryStateStore()
        plan = await create_logical_plan(
            "replan-owner", [_node("done"), _node("broken", depends_on=["done"]), _node("tail", depends_on=["broken"])],
        )
        first = materialize_frontier(plan, limit=1)
        first[0].status = TaskStatus.COMPLETED
        first[0].result = {"success": True, "content": "durable prefix"}
        await commit_frontier_results("replan-owner", plan, first)
        broken = materialize_frontier(plan, limit=1)
        broken[0].status = TaskStatus.FAILED
        broken[0].error = "method unavailable"
        broken[0].error_code = "CAPABILITY_UNAVAILABLE"
        await commit_frontier_results("replan-owner", plan, broken)

        job = Job(
            job_id="logical-replan",
            user_id="replan-owner",
            request="complete the task",
            scene="office",
            status=JobStatus.FAILED,
            nodes=broken,
            routing={"level": "m2", "logical_plan": {"plan_id": plan["plan_id"], "revision": 1}},
        )
        await store.create_job(job)
        orchestrator = AgentOrchestrator(
            store=store,
            planner=ReplacementPlanner(),
            workers={"test_worker": object()},
            temporal_enabled=False,
        )
        orchestrator._job_plan_context[job.job_id] = {
            "user_id": job.user_id,
            "request": job.request,
            "scene": job.scene,
            "project_id": None,
            "project_ids": None,
            "llm_api_key": None,
            "clarification_answer": None,
            "office_docs": None,
            "prior_summaries": "",
        }

        assert await orchestrator._maybe_replan_logical_plan(job, None) is True
        saved = await store.get_job(job.job_id)
        assert saved and saved.status == JobStatus.RUNNING
        assert len(saved.nodes) == 1
        assert saved.nodes[0].id.startswith("replan-2-")
        assert saved.nodes[0].metadata["logical_dependency_refs"]

    asyncio.run(scenario())


def test_logical_plan_replan_is_blocked_after_committed_effect():
    class NoopPlanner(Planner):
        async def plan(self, *args, **kwargs):
            raise AssertionError("effect boundary must stop before replanning")

    async def scenario():
        store = InMemoryStateStore()
        plan = await create_logical_plan("effect-owner", [_node("write"), _node("broken", depends_on=["write"])])
        first = materialize_frontier(plan, limit=1)
        first[0].status = TaskStatus.COMPLETED
        first[0].effect_status = "committed"
        first[0].result = {"success": True, "content": "already changed"}
        await commit_frontier_results("effect-owner", plan, first)
        broken = materialize_frontier(plan, limit=1)
        broken[0].status = TaskStatus.FAILED
        broken[0].error = "cannot continue"
        await commit_frontier_results("effect-owner", plan, broken)
        job = Job(
            job_id="effect-replan",
            user_id="effect-owner",
            request="continue",
            scene="office",
            status=JobStatus.FAILED,
            nodes=broken,
            routing={"level": "m2", "logical_plan": {"plan_id": plan["plan_id"]}},
        )
        await store.create_job(job)
        orchestrator = AgentOrchestrator(
            store=store,
            planner=NoopPlanner(),
            workers={"test_worker": object()},
            temporal_enabled=False,
        )

        assert await orchestrator._maybe_replan_logical_plan(job, None) is False
        saved = await store.get_job(job.job_id)
        assert saved and saved.routing["automatic_replan_blocked"] == "effectful_task"

    asyncio.run(scenario())
