"""Contract tests for the pure-read Temporal logical-plan migration.

No Temporal server is required.  The tests protect the separation between a
small Workflow payload and the Redis-resident complete plan, especially the
fact that a current frontier cannot hide a later write/ReAct node.
"""

import asyncio

from app.agents.orchestration.planning.logical_plan import (
    create_logical_plan,
    logical_plan_execution_fingerprint,
    replace_unfinished_tail,
)
from app.agents.orchestration.models import Job, TaskNode
from app.agents.orchestration.runtime.runtime_gateway import RuntimeGateway
from app.agents.orchestration.runtime.state import InMemoryStateStore


def _logical_job(plan_id: str, *, nodes: list[TaskNode] | None = None) -> Job:
    return Job(
        job_id="logical-read-job",
        user_id="user-1",
        request="分析资料",
        nodes=nodes or [TaskNode(id="current", agent="retrieval", params={"query": "资料"})],
        routing={"logical_plan": {"plan_id": plan_id, "revision": 1}},
    )


def test_logical_read_eligibility_checks_complete_plan_not_current_frontier():
    async def scenario():
        plan = await create_logical_plan(
            "user-1",
            [
                TaskNode(id="current", agent="retrieval", params={"query": "资料"}),
                TaskNode(id="later-write", agent="office_doc", params={"mode": "edit", "doc_id": "d1", "instruction": "改写"}),
            ],
        )
        job = _logical_job(plan["plan_id"])
        decision = RuntimeGateway.logical_read_rollout_eligibility(job, plan)
        assert decision.eligible is False
        assert decision.code == "mode_not_allowlisted"

    asyncio.run(scenario())


def test_logical_read_eligibility_rejects_react_and_plan_tampering():
    async def scenario():
        plan = await create_logical_plan(
            "user-1", [TaskNode(id="react", agent="react_step", params={"instruction": "查找"})]
        )
        job = _logical_job(plan["plan_id"])
        assert RuntimeGateway.logical_read_rollout_eligibility(job, plan).code == "agent_not_allowlisted"

        plan = await create_logical_plan(
            "user-1", [TaskNode(id="read", agent="retrieval", params={"query": "资料"})]
        )
        job = _logical_job(plan["plan_id"])
        plan["nodes"]["read"]["node"]["params"]["query"] = "被篡改"
        assert RuntimeGateway.logical_read_rollout_eligibility(job, plan).code == "logical_plan_fingerprint"

    asyncio.run(scenario())


def test_logical_read_rollout_is_disabled_by_default_and_honors_allowlist(monkeypatch):
    from app.core.config import settings

    async def scenario():
        plan = await create_logical_plan(
            "user-1", [TaskNode(id="read", agent="retrieval", params={"query": "资料"})]
        )
        job = _logical_job(plan["plan_id"])
        monkeypatch.setattr(settings, "TEMPORAL_LOGICAL_READ_PERCENTAGE", 0)
        assert RuntimeGateway.logical_read_rollout_eligibility(job, plan).code == "rollout_disabled"
        monkeypatch.setattr(settings, "TEMPORAL_LOGICAL_READ_ALLOWLIST", "user-1")
        assert RuntimeGateway.logical_read_rollout_eligibility(job, plan).eligible is True

    asyncio.run(scenario())


def test_logical_read_replacement_reseals_plan_and_keeps_only_reviewed_history():
    async def scenario():
        plan = await create_logical_plan(
            "user-1", [TaskNode(id="old", agent="retrieval", params={"query": "资料"})]
        )
        original_fingerprint = plan["execution_fingerprint"]
        replace_unfinished_tail(
            plan,
            [TaskNode(id="replacement", agent="retrieval", params={"query": "替代资料"})],
            reason="前沿失败",
            history_metadata={"runtime": "temporal_logical_read", "replan_count": 1},
        )
        assert plan["execution_fingerprint"] != original_fingerprint
        assert plan["execution_fingerprint"] == logical_plan_execution_fingerprint(plan)
        assert RuntimeGateway.logical_read_rollout_eligibility(
            _logical_job(plan["plan_id"]), plan
        ).code == "rollout_disabled"
        plan["history"].append({"runtime": "legacy"})
        assert RuntimeGateway.logical_read_rollout_eligibility(
            _logical_job(plan["plan_id"]), plan
        ).code == "logical_plan_replanned"

    asyncio.run(scenario())


def test_submit_logical_read_persists_job_but_sends_only_runtime_reference(monkeypatch):
    async def scenario():
        store = InMemoryStateStore()
        runtime = RuntimeGateway(store=store, temporal_mode=True)
        plan = await create_logical_plan(
            "user-1", [TaskNode(id="read", agent="retrieval", params={"query": "机密正文"})]
        )
        job = _logical_job(plan["plan_id"])
        captured = {}

        async def store_config(job_id, config):
            captured["config"] = (job_id, config)

        async def start_workflow(payload, job_id):
            captured["workflow"] = (payload, job_id)

        async def store_replan_context(job_id, context):
            captured["replan_context"] = (job_id, context)

        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.store_job_llm_config", store_config
        )
        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.start_logical_read_workflow", start_workflow
        )
        monkeypatch.setattr(
            "app.agents.orchestration.temporal.client.store_temporal_replan_context",
            store_replan_context,
        )
        await runtime.submit_logical_read(job, None, {"api_key": "secret", "model": "m"})
        assert (await store.get_job(job.job_id)).routing["runtime"] == "temporal_logical_read"
        payload, workflow_id = captured["workflow"]
        assert workflow_id == job.job_id
        assert set(payload) == {
            "job_id", "heartbeat_seconds", "frontier_timeout_seconds", "continue_after_frontiers"
        }
        assert "secret" not in repr(payload)
        assert "机密正文" not in repr(payload)
        assert captured["replan_context"][0] == job.job_id

    asyncio.run(scenario())


def test_logical_read_activity_does_not_execute_terminal_job(monkeypatch):
    from app.agents.orchestration.models import JobStatus
    from app.agents.orchestration.temporal import logical_read_activities

    terminal = Job(job_id="done", user_id="user-1", request="任务", status=JobStatus.CANCELLED)

    class FakeStore:
        async def get_job(self, job_id):
            assert job_id == "done"
            return terminal

    monkeypatch.setattr(logical_read_activities.frontier_read, "RedisStateStore", lambda: FakeStore())
    result = asyncio.run(logical_read_activities.run_logical_read_frontier_activity({"job_id": "done"}))
    assert result == {"terminal": True, "status": "cancelled"}
