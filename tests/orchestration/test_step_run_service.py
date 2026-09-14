"""resume run_next 单步执行服务（StepRunService）的回归测试。

覆盖动作本体：
  - routing.steps/current_step_index → Job.nodes 同 id TaskNode 定位；
  - 单节点执行（node executor/lifecycle 复用）与结果写回（result_ref/steps）；
  - canonical 推进：waiting_next / completed / failed / waiting_approval 事件；
  - step_resume 前置校验接入（状态/expected_step/幂等/依赖）。
"""

from __future__ import annotations

import asyncio

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore
from app.agents.orchestration.step_run_service import (
    EVENT_STEP_COMPLETED,
    EVENT_STEP_STARTED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_WAITING_NEXT,
    StepRunService,
)
from app.repositories.job_repository import StateStoreJobRepository


class _FakeWorker:
    def __init__(self, *, fail=False, output="ok", delay=0.0):
        self.calls = 0
        self.fail = fail
        self.output = output
        self.delay = delay

    async def execute(self, node: TaskNode, ctx) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("worker boom")
        return {"success": True, "content": self.output, "tool": "fake"}


def _make_store(*, nodes: list[TaskNode], state: str = "waiting_run", revision: int = 1,
                steps_statuses=None) -> tuple[StateStoreJobRepository, Job]:
    store = InMemoryStateStore()
    repo = StateStoreJobRepository(store)
    steps = []
    for node in nodes:
        status = "pending"
        if steps_statuses:
            status = str(steps_statuses.get(node.id) or "pending")
        steps.append({"id": node.id, "title": node.name or node.id, "description": "",
                      "domain": node.agent, "status": status, "result_ref": None})
    job = Job(
        job_id="plan-job-1",
        user_id="u1",
        user_role="user",
        request="逐步骤执行测试",
        scene="office",
        status=JobStatus.PENDING,
        nodes=nodes,
        routing={
            "execution_mode": "step_confirm",
            "execution_state": state,
            "plan_revision": revision,
            "current_step_index": 0,
            "steps": steps,
        },
    )
    return repo, job


def _service(*, repo, worker, **kwargs) -> StepRunService:
    hooks = {"finalized_completed": [], "finalized_failed": []}

    async def noop_capacity(_job) -> bool:
        return True

    async def finalize_completed(_job) -> None:
        hooks["finalized_completed"].append(_job.job_id)

    async def finalize_failed(_job) -> None:
        hooks["finalized_failed"].append(_job.job_id)

    service = StepRunService(
        store=repo,
        workers={"w1": worker},
        review=NoopReviewer(),
        finalizer=object(),
        llm_configs={},
        ensure_active_capacity=noop_capacity,
        finalize_completed=finalize_completed,
        finalize_failed=finalize_failed,
        poll_interval=0.02,
    )
    return service, hooks


async def _collect(service, *, job_id="plan-job-1", expected="", key="k1", bound=True):
    return [
        event
        async for event in service.run_next_stream(
            job_id=job_id,
            expected_step_id=expected,
            idempotency_key=key,
            workspace_bound=bound,
        )
    ]


def _waiting_step(nid: str, dep: str | None = None) -> TaskNode:
    node = TaskNode(
        id=nid,
        name=f"步骤 {nid}",
        agent="w1",
        params={"instruction": f"do {nid}"},
        depends_on=[dep] if dep else [],
    )
    return node


def _single_step_flow_success():
    async def scenario():
        repo, job = _make_store(nodes=[_waiting_step("s1"), _waiting_step("s2", "s1")])
        await repo.create_job(job)
        worker = _FakeWorker(output="step-1-out")
        service, hooks = _service(repo=repo, worker=worker)

        events = await _collect(service)
        types = [e["type"] for e in events]
        assert EVENT_STEP_STARTED in types
        assert EVENT_STEP_COMPLETED in types
        assert EVENT_WAITING_NEXT in types
        assert hooks["finalized_completed"] == []
        assert worker.calls == 1

        refreshed = await repo.get_job(job.job_id)
        assert refreshed.routing["execution_state"] == "waiting_next"
        assert refreshed.routing["current_step_index"] == 1
        assert refreshed.nodes[0].status == TaskStatus.COMPLETED
        steps = refreshed.routing["steps"]
        assert steps[0]["status"] == "completed"
        assert steps[0]["result_ref"]
        assert steps[1]["status"] == "pending"

        # 第二步 → 终态
        events2 = await _collect(service, key="k2")
        types2 = [e["type"] for e in events2]
        assert EVENT_TASK_COMPLETED in types2
        assert hooks["finalized_completed"] == [job.job_id]
        refreshed2 = await repo.get_job(job.job_id)
        assert refreshed2.routing["execution_state"] == "completed"
        assert refreshed2.status == JobStatus.COMPLETED
        assert all(n.status == TaskStatus.COMPLETED for n in refreshed2.nodes)
        assert worker.calls == 2

    asyncio.run(scenario())


def test_run_next_executes_single_step_and_converges():
    _single_step_flow_success()


def test_run_next_rejects_bad_state_expected_step_and_idempotency():
    async def scenario():
        # 非等待状态（planning 自动执行中）→ JOB_NOT_RESUMABLE
        repo, job = _make_store(
            nodes=[_waiting_step("s1")],
            state="planning",
        )
        await repo.create_job(job)
        service, _ = _service(repo=repo, worker=_FakeWorker())
        events = await _collect(service, key="x1")
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "JOB_NOT_RESUMABLE"

        # expected_step_id 不匹配 → STEP_MISMATCH
        repo2, job2 = _make_store(nodes=[_waiting_step("s1")])
        await repo2.create_job(job2)
        service2, _ = _service(repo=repo2, worker=_FakeWorker())
        events2 = await _collect(service2, expected="step-other", key="x2")
        assert events2[0]["code"] == "STEP_MISMATCH"

        # 同一幂等键第二次 → IDEMPOTENCY_DUPLICATE
        worker = _FakeWorker(output="once")
        repo3, job3 = _make_store(nodes=[_waiting_step("s1"), _waiting_step("s2", "s1")])
        await repo3.create_job(job3)
        service3, _ = _service(repo=repo3, worker=worker)
        await _collect(service3, key="dup-1")
        events3 = await _collect(service3, key="dup-1")
        assert events3[0]["type"] == "error"
        assert events3[0]["code"] == "IDEMPOTENCY_DUPLICATE"
        assert worker.calls == 1  # 未重复执行

    asyncio.run(scenario())


def test_run_next_step_dependencies_and_workspace_bound():
    async def scenario():
        # 依赖未完成 → STEP_DEPENDENCIES_NOT_MET
        repo, job = _make_store(nodes=[_waiting_step("s1"), _waiting_step("s2", "s1")],
                                steps_statuses={"s2": "pending"})
        job.routing["current_step_index"] = 1
        await repo.create_job(job)
        service, _ = _service(repo=repo, worker=_FakeWorker())
        events = await _collect(service, key="dep-1")
        assert events[0]["code"] == "STEP_DEPENDENCIES_NOT_MET"

        # 工作区绑定失效 → WORKSPACE_REBOUND
        repo2, job2 = _make_store(nodes=[_waiting_step("s1")])
        await repo2.create_job(job2)
        service2, _ = _service(repo=repo2, worker=_FakeWorker())
        events2 = await _collect(service2, key="ws-1", bound=False)
        assert events2[0]["code"] == "WORKSPACE_REBOUND"

    asyncio.run(scenario())


def test_run_next_failure_terminates_job():
    async def scenario():
        repo, job = _make_store(nodes=[_waiting_step("s1")])
        await repo.create_job(job)
        service, hooks = _service(repo=repo, worker=_FakeWorker(fail=True))
        events = await _collect(service, key="fail-1")
        types = [e["type"] for e in events]
        assert EVENT_TASK_FAILED in types
        assert hooks["finalized_failed"] == [job.job_id]
        refreshed = await repo.get_job(job.job_id)
        assert refreshed.status == JobStatus.FAILED
        assert refreshed.routing["execution_state"] == "failed"
        assert refreshed.routing["steps"][0]["status"] == "failed"

    asyncio.run(scenario())


def test_run_next_concurrent_lock_guards_second_request():
    async def scenario():
        repo, job = _make_store(nodes=[_waiting_step("s1", )])
        await repo.create_job(job)
        worker = _FakeWorker(output="slow", delay=0.2)
        service, _ = _service(repo=repo, worker=worker)

        async def first():
            return [e async for e in service.run_next_stream(job_id=job.job_id, idempotency_key="c1")]

        async def second():
            await asyncio.sleep(0.05)
            return [e async for e in service.run_next_stream(job_id=job.job_id, idempotency_key="c2")]

        events_first, events_second = await asyncio.gather(first(), second())
        assert any(e["type"] == EVENT_TASK_COMPLETED for e in events_first)
        assert events_second[0]["type"] == "error"
        assert events_second[0]["code"] == "STEP_ALREADY_RUNNING"
        assert worker.calls == 1

    asyncio.run(scenario())


def test_run_next_on_terminal_job_replays_terminal_event():
    async def scenario():
        repo, job = _make_store(nodes=[_waiting_step("s1")], state="completed")
        job.status = JobStatus.COMPLETED
        job.nodes[0].status = TaskStatus.COMPLETED
        job.nodes[0].result = {"success": True, "content": "done-text"}
        job.routing["steps"][0]["status"] = "completed"
        await repo.create_job(job)
        service, _ = _service(repo=repo, worker=_FakeWorker())
        events = await _collect(service, key="term-1")
        assert events[0]["type"] == EVENT_TASK_COMPLETED

    asyncio.run(scenario())
