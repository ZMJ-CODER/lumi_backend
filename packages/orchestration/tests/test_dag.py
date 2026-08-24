import asyncio
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from lumi_orch.dag import DagValidationError, decide_next_nodes, validate_dag
from lumi_orch.escalation import EscalationLevel, coerce_escalation
from lumi_orch.errors import ErrorCategory, OrchestrationError, classify_error
from lumi_orch.lifecycle import InvalidStateTransition, can_transition, transition
from lumi_orch.policies import is_terminal, may_escalate, may_retry
from lumi_orch.replanning import decide_failed_job_replan, decide_logical_plan_replan
from lumi_orch.logical_plan import logical_plan_progress, select_budgeted_frontier
from lumi_orch.manifest import advance_cursor, manifest_progress as manifest_progress_kernel, next_manifest_batch
from lumi_orch.plan_dsl import PlanStep
from lumi_orch.resources import ResourceClaim, ResourceCoordinator
from lumi_orch.runner import ChannelLimiter, resolve_node_timeout


@dataclass
class Node:
    id: str
    depends_on: list[str] = field(default_factory=list)


def test_validate_dag_accepts_a_valid_graph():
    validate_dag([Node("read"), Node("analyse", ["read"])])


@pytest.mark.parametrize("nodes", [
    [Node("a"), Node("a")],
    [Node("a", ["missing"])],
    [Node("a", ["b"]), Node("b", ["a"])],
])
def test_validate_dag_rejects_invalid_graphs(nodes):
    with pytest.raises(DagValidationError):
        validate_dag(nodes)


def test_kernel_timeout_selection_never_reads_global_settings():
    timeout = resolve_node_timeout(
        {"params": {"preferred_tool": "read_document"}, "metadata": {"route_channel": "rag"}},
        default_seconds=60,
        channel_timeouts={"rag": 90},
        tool_timeouts={"read_document": 20},
    )

    assert timeout == 20


def test_kernel_models_validate_resource_and_escalation_data():
    assert ResourceClaim(key="document:1", mode="read").mode == "read"
    signal = coerce_escalation({"level": "task", "reason": "missing_prerequisite"}, default_node_id="node-1")

    assert signal is not None
    assert signal.level == EscalationLevel.TASK
    assert signal.affected_node_ids == ["node-1"]


def test_scheduler_allows_manifest_entries_to_continue_after_a_failed_dependency():
    failed = Node("failed")
    failed.status = "failed"
    strict = Node("strict", ["failed"])
    tolerant = Node("tolerant", ["failed"])
    tolerant.metadata = {"continue_on_dependency_failure": True}

    decision = decide_next_nodes(
        {node.id: node for node in (failed, strict, tolerant)},
        pending_ids={"strict", "tolerant"},
        completed_ids=set(),
        settled_ids={"failed"},
    )

    assert decision.skip_ids == ("strict",)
    assert decision.ready_ids == ("tolerant",)


def test_channel_limiter_uses_local_limit_when_no_redis_backend_is_provided():
    async def scenario():
        limiter = ChannelLimiter(limit_provider=lambda _channel: 1)
        entered = 0
        max_entered = 0

        async def task():
            nonlocal entered, max_entered
            async with limiter.claim("agent"):
                entered += 1
                max_entered = max(max_entered, entered)
                await asyncio.sleep(0)
                entered -= 1

        await asyncio.gather(task(), task())
        assert max_entered == 1

    asyncio.run(scenario())


def test_channel_lease_renewal_failure_cancels_the_holder(monkeypatch):
    """A lost distributed slot must stop work before another owner may run."""
    import lumi_orch.runner as runner_module

    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, _keys, *_args):
            self.calls.append(script)
            return 0 if script == runner_module.CHANNEL_RENEW_SCRIPT else 1

        async def zrem(self, _key, _token):
            return 1

    native_sleep = asyncio.sleep

    async def yield_immediately(_seconds):
        await native_sleep(0)

    monkeypatch.setattr(runner_module.asyncio, "sleep", yield_immediately)
    redis = FakeRedis()
    limiter = ChannelLimiter(redis_provider=lambda: redis, limit_provider=lambda _channel: 1)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            async with limiter.claim("agent", lease_seconds=60):
                while True:
                    await native_sleep(0)

    asyncio.run(scenario())
    assert runner_module.CHANNEL_ACQUIRE_SCRIPT in redis.calls
    assert runner_module.CHANNEL_RENEW_SCRIPT in redis.calls


def test_resource_lease_renewal_failure_cancels_the_holder(monkeypatch):
    """Write/read resource ownership loss must not leave a tool running."""
    import lumi_orch.resources as resources_module

    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, _keys, *_args):
            self.calls.append(script)
            return 0 if script == resources_module._RENEW_SCRIPT else 1

    native_sleep = asyncio.sleep

    async def yield_immediately(_seconds):
        await native_sleep(0)

    monkeypatch.setattr(resources_module.asyncio, "sleep", yield_immediately)
    redis = FakeRedis()
    coordinator = ResourceCoordinator(redis_provider=lambda: redis)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            async with coordinator.claim([ResourceClaim(key="doc:1", mode="write")], ttl=3):
                while True:
                    await native_sleep(0)

    asyncio.run(scenario())
    assert resources_module._ACQUIRE_SCRIPT in redis.calls
    assert resources_module._RENEW_SCRIPT in redis.calls


def test_lifecycle_rejects_terminal_job_mutation_without_knowing_job_model():
    @dataclass
    class Job:
        status: str

    job = Job("running")
    assert can_transition(job.status, "paused")
    transition(job, "paused")
    assert job.status == "paused"
    with pytest.raises(InvalidStateTransition):
        transition(Job("completed"), "running")


def test_error_taxonomy_preserves_explicit_recovery_metadata():
    class CapacityError(OrchestrationError):
        category = ErrorCategory.CAPACITY
        code = "CAPACITY_FULL"
        retryable = True

    info = classify_error(CapacityError("full"))

    assert info.category == ErrorCategory.CAPACITY
    assert info.code == "CAPACITY_FULL"
    assert info.retryable is True


def test_kernel_retry_and_terminal_policies_work_with_any_node_shape():
    @dataclass
    class RetryNode:
        retries: int = 0
        max_retries: int = 1
        status: str = "failed"

    timeout = type("Timeout", (), {
        "category": ErrorCategory.TIMEOUT,
        "retryable": True,
        "replannable": True,
    })()

    assert is_terminal("completed")
    assert may_retry(RetryNode(), timeout)
    assert may_escalate(timeout)


def test_replanning_policy_blocks_effects_and_upgrades_m0_without_app_enums():
    decision = decide_failed_job_replan(
        may_upgrade=True,
        target="m1",
        category="capability_error",
        current="m0",
        upgrade_count=0,
        replan_count=0,
        max_replans=2,
    )
    assert decision.allowed and decision.target == "m2"
    blocked = decide_logical_plan_replan(
        dynamic_enabled=True, replan_count=0, max_replans=2, effectful=True
    )
    assert blocked.allowed is False
    assert blocked.blocked_code == "effectful_task"


def test_logical_plan_frontier_requires_completed_dependencies_and_honors_budget():
    records = {
        "read": {"status": "completed", "estimated_tokens": 10, "node": {"depends_on": []}},
        "analyse": {"status": "pending", "estimated_tokens": 20, "node": {"depends_on": ["read"]}},
        "write": {"status": "pending", "estimated_tokens": 30, "node": {"depends_on": ["analyse"]}},
    }
    selected = select_budgeted_frontier(records, ["read", "analyse", "write"], limit=2, reserved=0, used=0, ceiling=25)

    assert selected.node_ids == ("analyse",)
    assert selected.reserved_increment == 20
    assert logical_plan_progress(records).pending == 2


def test_manifest_cursor_math_is_bounded_and_does_not_advance_on_non_pending_items():
    items = [{"status": "completed"}, {"status": "pending"}, {"status": "pending"}]
    progress = manifest_progress_kernel(items, -10)

    assert progress.cursor == 0
    assert next_manifest_batch(items, cursor=1, batch_size=1) == [items[1]]
    assert advance_cursor(1, total=3, settled_items=8) == 3


def test_plan_dsl_rejects_an_unbounded_or_invalid_risk_contract():
    step = PlanStep.model_validate({
        "id": "analyse",
        "action": "analyse",
        "output_contract": {"artifact_type": "text"},
    })

    assert step.risk_level == "read_only"
    with pytest.raises(ValidationError):
        PlanStep.model_validate({
            "id": "unsafe", "action": "send", "risk_level": "anything", "output_contract": {"artifact_type": "text"},
        })
