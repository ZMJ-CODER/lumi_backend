"""Contract tests for the opt-in Temporal approved-effects logical runtime.

The suite deliberately does not need a Temporal service.  It verifies the
admission boundary and the compact Workflow submission contract before this
runtime is enabled in any deployment.
"""

import asyncio

from app.agents.orchestration.backends.temporal_logical_effects import TemporalLogicalEffectsBackend
from app.agents.orchestration.planning.logical_plan import create_logical_plan
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.runtime.runtime_gateway import RuntimeGateway
from app.agents.orchestration.runtime.state import InMemoryStateStore


def _job(plan_id: str, *, user_id: str = "effects-user") -> Job:
    return Job(
        job_id="logical-effects-job",
        user_id=user_id,
        request="更新待办事项",
        nodes=[TaskNode(id="current", agent="office_todo", params={"action": "create"})],
        routing={"logical_plan": {"plan_id": plan_id, "revision": 1}},
    )


def _approved_write(node_id: str = "write") -> TaskNode:
    return TaskNode(
        id=node_id,
        agent="office_todo",
        params={"action": "create", "title": "跟进客户"},
        approval=True,
        idempotency_key=f"effect:{node_id}",
        resource_claims=[ResourceClaim(key="todo:effects-user", mode="write")],
    )


def test_logical_effects_requires_approved_idempotent_effect_and_rejects_react():
    async def scenario():
        approved_plan = await create_logical_plan("effects-user", [_approved_write()])
        decision = RuntimeGateway.logical_effects_rollout_eligibility(_job(approved_plan["plan_id"]), approved_plan)
        assert decision.code == "rollout_disabled"

        missing_approval = _approved_write("missing-approval")
        missing_approval.approval = False
        plan = await create_logical_plan("effects-user", [missing_approval])
        assert RuntimeGateway.logical_effects_rollout_eligibility(_job(plan["plan_id"]), plan).code == "effect_without_approval"

        missing_key = _approved_write("missing-key")
        missing_key.idempotency_key = None
        plan = await create_logical_plan("effects-user", [missing_key])
        assert RuntimeGateway.logical_effects_rollout_eligibility(_job(plan["plan_id"]), plan).code == "effect_without_idempotency"

        react_plan = await create_logical_plan(
            "effects-user", [TaskNode(id="react", agent="react_step", params={"instruction": "修改待办"})]
        )
        assert RuntimeGateway.logical_effects_rollout_eligibility(_job(react_plan["plan_id"]), react_plan).code == "agent_not_allowlisted"

    asyncio.run(scenario())


def test_logical_effects_does_not_claim_pure_read_plan(monkeypatch):
    from app.core.config import settings

    async def scenario():
        plan = await create_logical_plan(
            "effects-user", [TaskNode(id="read", agent="retrieval", params={"query": "资料"})]
        )
        monkeypatch.setattr(settings, "TEMPORAL_LOGICAL_EFFECTS_PERCENTAGE", 100)
        decision = RuntimeGateway.logical_effects_rollout_eligibility(_job(plan["plan_id"]), plan)
        assert decision.eligible is False
        assert decision.code == "no_declared_effect"

    asyncio.run(scenario())


def test_logical_effects_rollout_honors_allowlist(monkeypatch):
    from app.core.config import settings

    async def scenario():
        plan = await create_logical_plan("effects-user", [_approved_write()])
        job = _job(plan["plan_id"])
        monkeypatch.setattr(settings, "TEMPORAL_LOGICAL_EFFECTS_ALLOWLIST", "effects-user")
        assert RuntimeGateway.logical_effects_rollout_eligibility(job, plan).eligible is True
        monkeypatch.setattr(settings, "TEMPORAL_LOGICAL_EFFECTS_ALLOWLIST", "other-user")
        assert RuntimeGateway.logical_effects_rollout_eligibility(job, plan).code == "rollout_not_allowlisted"

    asyncio.run(scenario())


def test_submit_logical_effects_sends_reference_only_and_never_exposes_key(monkeypatch):
    async def scenario():
        store = InMemoryStateStore()
        runtime = RuntimeGateway(store=store, temporal_mode=True)
        plan = await create_logical_plan("effects-user", [_approved_write()])
        job = _job(plan["plan_id"])
        captured = {}

        async def store_config(job_id, config):
            captured["config"] = (job_id, config)

        async def start_workflow(payload, job_id):
            captured["workflow"] = (payload, job_id)

        monkeypatch.setattr("app.agents.orchestration.temporal.client.store_job_llm_config", store_config)
        monkeypatch.setattr("app.agents.orchestration.temporal.client.start_logical_effects_workflow", start_workflow)

        await runtime.submit_logical_effects(job, None, {"api_key": "secret", "model": "m"})

        assert (await store.get_job(job.job_id)).routing["runtime"] == "temporal_logical_effects"
        payload, workflow_id = captured["workflow"]
        assert workflow_id == job.job_id
        assert set(payload) == {
            "job_id", "heartbeat_seconds", "frontier_timeout_seconds", "continue_after_frontiers"
        }
        assert "secret" not in repr(payload)
        assert "跟进客户" not in repr(payload)

    asyncio.run(scenario())


def test_logical_effects_control_never_falls_back_when_temporal_is_unavailable():
    async def scenario():
        store = InMemoryStateStore()
        backend = TemporalLogicalEffectsBackend(RuntimeGateway(store=store, temporal_mode=False))
        job = Job(
            job_id="effects-control",
            user_id="effects-user",
            request="更新待办",
            status=JobStatus.WAITING_APPROVAL,
            routing={"runtime": "temporal_logical_effects"},
            nodes=[_approved_write()],
        )
        result = await backend.approve(job, "write", True)
        assert result is not None
        assert result.handled is True
        assert result.error and "未送达" in result.error
        assert job.status == JobStatus.WAITING_APPROVAL

    asyncio.run(scenario())


def test_logical_effects_workflow_consumes_approval_and_resume_can_wake_wait():
    """A capacity-paused approval must not leave the Workflow wait stuck."""
    from app.agents.temporal_logical_effects_workflows import LogicalEffectsWorkflow

    workflow = LogicalEffectsWorkflow()
    asyncio.run(workflow.approve_task({"node_id": "write", "approved": True}))
    assert workflow._approvals == [{"node_id": "write", "approved": True}]
    assert workflow._wake_generation == 1
    workflow._consume_approval({"node_id": "write", "approved": True})
    assert workflow._approvals == []
    asyncio.run(workflow.resume())
    assert workflow._wake_generation == 2


def test_logical_effects_approval_expiry_activity_is_safe_for_missing_job(monkeypatch):
    from app.agents.orchestration.temporal import logical_read_activities

    class FakeStore:
        async def get_job(self, job_id):
            assert job_id == "missing"
            return None

    monkeypatch.setattr(logical_read_activities.approval, "RedisStateStore", lambda: FakeStore())
    result = asyncio.run(
        logical_read_activities.expire_logical_effects_approval_activity({"job_id": "missing"})
    )
    assert result == {"expired": False, "reason": "job_not_found"}


def test_logical_effects_approval_expiry_activity_persists_timeout(monkeypatch):
    from app.agents.orchestration.temporal import logical_read_activities

    async def scenario():
        store = InMemoryStateStore()
        node = _approved_write()
        node.status = TaskStatus.PENDING
        node.metadata = {
            "awaiting_approval": True,
            "approval_tool": "todo_manager",
            "approval_fingerprint": "fixed-fingerprint",
            "approval_expires_at": 1,
        }
        job = Job(
            job_id="expired-effects",
            user_id="effects-user",
            request="更新待办",
            status=JobStatus.WAITING_APPROVAL,
            routing={"runtime": "temporal_logical_effects"},
            nodes=[node],
        )
        await store.create_job(job)
        monkeypatch.setattr(logical_read_activities.approval, "RedisStateStore", lambda: store)
        result = await logical_read_activities.expire_logical_effects_approval_activity(
            {"job_id": job.job_id}
        )
        persisted = await store.get_job(job.job_id)
        assert result == {"expired": True, "status": "failed"}
        assert persisted is not None and persisted.status == JobStatus.FAILED
        assert persisted.nodes[0].error_code == "APPROVAL_TIMEOUT"

    asyncio.run(scenario())


def test_logical_effects_cancel_activity_persists_waiting_gate_as_cancelled(monkeypatch):
    from app.agents.orchestration.temporal import logical_read_activities

    async def scenario():
        store = InMemoryStateStore()
        node = _approved_write()
        node.status = TaskStatus.PENDING
        node.metadata = {"awaiting_approval": True}
        job = Job(
            job_id="cancelled-effects",
            user_id="effects-user",
            request="更新待办",
            status=JobStatus.WAITING_APPROVAL,
            routing={"runtime": "temporal_logical_effects"},
            nodes=[node],
        )
        await store.create_job(job)
        monkeypatch.setattr(logical_read_activities.lifecycle, "RedisStateStore", lambda: store)
        result = await logical_read_activities.cancel_logical_effects_job_activity(
            {"job_id": job.job_id, "keep_completed": True}
        )
        persisted = await store.get_job(job.job_id)
        assert result == {"cancelled": True, "status": "cancelled"}
        assert persisted is not None and persisted.status == JobStatus.CANCELLED
        assert persisted.nodes[0].status == TaskStatus.CANCELLED
        assert persisted.nodes[0].error_code == "JOB_CANCELLED"

    asyncio.run(scenario())
