"""EXECUTION_PLAN_FIRST 启用验证：计划优先 + resume run_next 逐步骤驱动。

打开计划优先开关后，step_confirm 办公任务提交即返回计划并置 waiting_run、
不自动派发任何工具；由 /agents/jobs/{id}/resume（action=run_next）逐步骤
执行，直到任务收敛（waiting_next → … → task_completed）。
"""

from __future__ import annotations

import asyncio

from deadline import with_deadline

from app.agents.orchestration.models import JobStatus, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planning.planner import Planner, TaskTree
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore


class _FakeWorker:
    def __init__(self):
        self.calls = []

    async def execute(self, node, ctx):
        self.calls.append(node.id)
        return {"success": True, "content": f"result-{node.id}", "tool": "fake"}


def _planner(nodes: list) -> Planner:
    class FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=list(nodes), plan_text="计划文本：{n}".format(n=len(nodes)))

        async def plan_for_level(self, *_args, **_kwargs):
            return TaskTree(nodes=list(nodes), plan_text="计划文本")

    return FakePlanner()


def _node(nid, dep=None):
    from app.agents.orchestration.models import TaskNode

    return TaskNode(id=nid, name=nid, agent="w1", depends_on=[dep] if dep else [])


def test_plan_first_submit_parks_then_run_next_drives_to_completion(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "EXECUTION_PLAN_FIRST", True)
    monkeypatch.setattr(settings, "AGENT_LLM_MAX_CONCURRENCY", 8)

    store = InMemoryStateStore()
    worker = _FakeWorker()
    orch = AgentOrchestrator(
        store=store,
        planner=_planner([_node("s1"), _node("s2", "s1")]),
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )
    # 终态记录避免触碰外部 Redis/Postgres（与既有编排测试同策略）。
    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_record_office_summary", noop)
    monkeypatch.setattr(orch, "_record_office_task_index", noop)
    monkeypatch.setattr(orch, "_discard_pending_learning", lambda *_a, **_k: None)

    async def scenario():
        job = await orch.submit_job(
            "u1", "逐步骤处理", conversation_id="c1", execution_preference="step_confirm",
        )

        # 1) 计划优先停放：不自动派发（无后台任务），canonical=waiting_run
        assert job.status == JobStatus.PENDING
        assert job.routing["execution_state"] == "waiting_run"
        assert job.routing["execution_mode"] == "step_confirm"
        assert orch._tasks == {}
        parked = await orch.get_job(job.job_id)
        assert parked.routing["execution_state"] == "waiting_run"
        assert len(parked.routing["steps"]) == 2

        # 2) run_next 执行第一步 → waiting_next
        events_1 = [e async for e in orch.stream_run_next(job_id=job.job_id, idempotency_key="k1")]
        kinds_1 = {e["type"] for e in events_1}
        assert "step_started" in kinds_1
        assert "waiting_next" in kinds_1
        mid = await orch.get_job(job.job_id)
        assert mid.routing["execution_state"] == "waiting_next"
        assert mid.routing["steps"][0]["status"] == "completed"
        assert mid.nodes[0].status == TaskStatus.COMPLETED
        assert worker.calls == ["s1"]

        # 3) run_next 执行第二步 → task_completed（终态收敛）
        events_2 = [e async for e in orch.stream_run_next(job_id=job.job_id, idempotency_key="k2")]
        kinds_2 = {e["type"] for e in events_2}
        assert "task_completed" in kinds_2
        final = await orch.get_job(job.job_id)
        assert final.status == JobStatus.COMPLETED
        assert final.routing["execution_state"] == "completed"
        assert worker.calls == ["s1", "s2"]
        return final

    final = asyncio.run(
        with_deadline(
            scenario(), label="计划优先 submit+run_next 收敛",
            # 走到真实 submit_job：离线环境下要先等 MCP 能力发现超时再降级。
            tier="m1",
        )
    )
    assert final.result is not None
    answer = str(
        (final.result or {}).get("final_answer")
        or (final.result or {}).get("answer")
        or ""
    )
    assert "result-s1" in answer and "result-s2" in answer


def test_plan_first_approve_parks_instead_of_auto_running_whole_dag(monkeypatch):
    """计划优先任务的高风险步骤审批只解门闩，不自动恢复整 DAG。"""
    store = InMemoryStateStore()
    worker = _FakeWorker()
    orch = AgentOrchestrator(
        store=store,
        planner=_planner([_node("s1"), _node("s2", "s1")]),
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus

        node_s1 = TaskNode(
            id="s1", name="审批步骤", agent="w1",
            metadata={
                "awaiting_approval": True,
                "approval_tool": "fake_tool",
                "approval_fingerprint": "fp-1",
                "confirmed_tools": [],
                "confirmed_tool_calls": [],
            },
            status=TaskStatus.PENDING,
        )
        node_s2 = TaskNode(id="s2", name="后续步骤", agent="w1", depends_on=["s1"])
        job = Job(
            job_id="approve-plan-job",
            user_id="u1",
            user_role="user",
            request="审批单步任务",
            scene="office",
            status=JobStatus.WAITING_APPROVAL,
            nodes=[node_s1, node_s2],
            routing={
                "execution_mode": "step_confirm",
                "execution_state": "waiting_approval",
                "plan_revision": 1,
                "current_step_index": 0,
                "steps": [
                    {"id": "s1", "title": "审批步骤", "description": "", "domain": "w1",
                     "status": "waiting_approval", "result_ref": None},
                    {"id": "s2", "title": "后续步骤", "description": "", "domain": "w1",
                     "status": "pending", "result_ref": None},
                ],
            },
        )
        await store.create_job(job)

        await orch.approve_job(job.job_id, "s1", approved=True)

        # 审批后不自动启动任何执行任务，等待 run_next 重新进入单步。
        assert orch._tasks == {}
        parked = await orch.get_job(job.job_id)
        assert parked.routing["execution_state"] == "waiting_next"
        assert parked.status == JobStatus.PENDING
        assert parked.nodes[0].status == TaskStatus.PENDING
        assert worker.calls == []

        # 随后的 run_next 正常执行已放行的当前步骤
        events = [e async for e in orch.stream_run_next(job_id=job.job_id, idempotency_key="app-k1")]
        assert any(e["type"] == "waiting_next" for e in events)
        assert worker.calls == ["s1"]

    asyncio.run(scenario())


def test_plan_first_run_next_rejects_wrong_step_view_and_duplicate_key(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "EXECUTION_PLAN_FIRST", True)
    store = InMemoryStateStore()
    worker = _FakeWorker()
    orch = AgentOrchestrator(
        store=store,
        planner=_planner([_node("s1"), _node("s2", "s1")]),
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job(
            "u1", "处理任务", conversation_id="c1", execution_preference="step_confirm",
        )
        assert job.routing["execution_state"] == "waiting_run"

        # 过期/错误视图：期望步骤与当前步骤不一致
        events = [e async for e in orch.stream_run_next(
            job_id=job.job_id, expected_step_id="s2", idempotency_key="k1"
        )]
        assert events[0]["type"] == "error"
        assert events[0]["code"] == "STEP_MISMATCH"
        assert worker.calls == []

        # 正确执行第一步后再用同一幂等键重放 → IDEMPOTENCY_DUPLICATE
        events_ok = [e async for e in orch.stream_run_next(job_id=job.job_id, idempotency_key="k1")]
        assert any(e["type"] == "waiting_next" for e in events_ok)
        dup = [e async for e in orch.stream_run_next(job_id=job.job_id, idempotency_key="k1")]
        assert dup[0]["code"] == "IDEMPOTENCY_DUPLICATE"
        assert worker.calls == ["s1"]

    asyncio.run(
        with_deadline(
            scenario(), label="计划优先 run_next 视图校验与幂等重放", tier="m1",
        )
    )
