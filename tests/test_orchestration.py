"""多智能体编排框架测试：DAG 校验 / 拓扑执行 / 重试 / 取消 / 编排器."""

import asyncio

import pytest

from app.agents.orchestration.dag import DagValidationError, execute_dag, validate_dag
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import (
    ActiveConversationJobError,
    AgentOrchestrator,
    UserJobLimitError,
)
from app.agents.orchestration.planner import Planner, TaskTree
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore
from app.agents.orchestration.workers import WORKERS, WorkerContext, list_workers
from app.agents.roles.atomic import AtomicStepAgent


def _node(nid, agent="w1", deps=(), **kw):
    return TaskNode(id=nid, name=nid, agent=agent, depends_on=list(deps), **kw)


class FakeWorker:
    def __init__(self, name="w1", fail_times=0, delay=0, result=None):
        self.name = name
        self.fail_times = fail_times
        self.calls = 0
        self.delay = delay
        self.result = result or {"success": True, "content": f"result-{name}"}

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return self.result


def _make_store_job(nodes):
    store = InMemoryStateStore()
    job = Job(job_id="j1", user_id="u1", request="test", nodes=nodes, status=JobStatus.RUNNING)
    return store, job


def test_validate_dag_rejects_cycle_and_missing_dep():
    with pytest.raises(DagValidationError):
        validate_dag([_node("a", deps=["b"]), _node("b", deps=["a"])])
    with pytest.raises(DagValidationError):
        validate_dag([_node("a", deps=["missing"])])
    validate_dag([_node("a"), _node("b", deps=["a"])])  # 合法不报错


def test_execute_dag_topological_order():
    workers = {"w1": FakeWorker("w1"), "w2": FakeWorker("w2"), "w3": FakeWorker("w3")}
    nodes = [
        _node("t1", agent="w1"),
        _node("t2", agent="w2", deps=["t1"]),
        _node("t3", agent="w3", deps=["t1"]),
        _node("t4", agent="w1", deps=["t2", "t3"]),
    ]
    store, job = _make_store_job(nodes)
    asyncio.run(execute_dag(job, workers, NoopReviewer(), store, concurrency=3))
    final = asyncio.run(store.get_job("j1"))
    assert all(n.status == TaskStatus.COMPLETED for n in final.nodes)
    assert final.status == JobStatus.COMPLETED
    assert final.nodes[-1].result["content"] == "result-w1"
    # t4 依赖 t2/t3，其完成时间必然晚于 t2/t3
    assert final.nodes[3].completed_at >= final.nodes[1].completed_at
    assert final.nodes[3].completed_at >= final.nodes[2].completed_at


def test_execute_dag_wakes_dependent_without_batch_barrier():
    """短分支完成后应立即唤醒后继，不等待同批无关慢分支."""
    events = []

    class TimedWorker:
        def __init__(self, name, delay):
            self.name = name
            self.delay = delay

        async def execute(self, node, ctx):
            events.append((node.id, "start", asyncio.get_running_loop().time()))
            await asyncio.sleep(self.delay)
            events.append((node.id, "end", asyncio.get_running_loop().time()))
            return {"success": True, "content": node.id}

    nodes = [
        _node("fast", agent="fast"),
        _node("slow", agent="slow"),
        _node("child", agent="child", deps=["fast"]),
    ]
    store, job = _make_store_job(nodes)
    asyncio.run(
        execute_dag(
            job,
            {
                "fast": TimedWorker("fast", 0.05),
                "slow": TimedWorker("slow", 0.35),
                "child": TimedWorker("child", 0.01),
            },
            NoopReviewer(),
            store,
            concurrency=3,
        )
    )
    times = {(nid, phase): at for nid, phase, at in events}
    assert times[("child", "start")] < times[("slow", "end")]


def test_execute_dag_retry_then_success():
    worker = FakeWorker("w1", fail_times=1)
    store, job = _make_store_job([_node("t1", agent="w1")])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert final.nodes[0].status == TaskStatus.COMPLETED
    assert final.nodes[0].retries == 1
    assert worker.calls == 2


def test_execute_dag_retry_exhausted_fails():
    worker = FakeWorker("w1", fail_times=99)
    store, job = _make_store_job([_node("t1", agent="w1")])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert final.nodes[0].status == TaskStatus.FAILED
    assert final.nodes[0].retries == final.nodes[0].max_retries
    assert final.status == JobStatus.FAILED


def test_same_resource_writes_are_serialized():
    active = 0
    max_active = 0

    class Writer:
        async def execute(self, node, ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {"success": True, "content": node.id}

    nodes = [
        _node(
            "a",
            resource_claims=[ResourceClaim(key="user:u1:office-doc:d1", mode="write")],
            idempotency_key="write-a",
        ),
        _node(
            "b",
            resource_claims=[ResourceClaim(key="user:u1:office-doc:d1", mode="write")],
            idempotency_key="write-b",
        ),
    ]
    store, job = _make_store_job(nodes)
    asyncio.run(execute_dag(job, {"w1": Writer()}, NoopReviewer(), store, concurrency=2))
    assert max_active == 1


def test_different_resource_writes_can_run_in_parallel():
    active = 0
    max_active = 0

    class Writer:
        async def execute(self, node, ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {"success": True, "content": node.id}

    nodes = [
        _node(
            "a",
            resource_claims=[ResourceClaim(key="user:u1:office-doc:d1", mode="write")],
            idempotency_key="parallel-a",
        ),
        _node(
            "b",
            resource_claims=[ResourceClaim(key="user:u1:office-doc:d2", mode="write")],
            idempotency_key="parallel-b",
        ),
    ]
    store, job = _make_store_job(nodes)
    asyncio.run(execute_dag(job, {"w1": Writer()}, NoopReviewer(), store, concurrency=2))
    assert max_active == 2


def test_effectful_node_is_not_retried_after_failure():
    worker = FakeWorker("w1", fail_times=99)
    node = _node(
        "effect",
        max_retries=2,
        resource_claims=[ResourceClaim(key="user:u1:todo", mode="write")],
        idempotency_key="effect-no-retry",
    )
    store, job = _make_store_job([node])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert worker.calls == 1
    assert final.nodes[0].status == TaskStatus.FAILED
    assert final.nodes[0].effect_status == "uncertain"


def test_atomic_step_requires_planned_tool():
    node = TaskNode(
        id="atomic",
        name="atomic",
        agent="atomic_step",
        params={"instruction": "执行一个步骤"},
    )
    result = asyncio.run(
        AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1"))
    )
    assert result["success"] is False
    assert result["error_code"] == "TOOL_NOT_PLANNED"


def test_atomic_step_switches_to_fallback_tool_on_retry(monkeypatch):
    import app.agents.roles.atomic as atomic_mod

    selected = {}

    class FakeLLM:
        async def chat_with_tools(self, messages, tools, **kwargs):
            selected["tool"] = tools[0]["function"]["name"]
            return "", [
                {
                    "id": "call-1",
                    "function": {"name": selected["tool"], "arguments": "{}"},
                }
            ]

        async def chat(self, messages, **kwargs):
            return "ok"

    async def fake_tools(scene, user_role):
        return [
            {"type": "function", "function": {"name": "office_doc_read", "parameters": {}}},
            {"type": "function", "function": {"name": "python_exec", "parameters": {}}},
        ]

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        return SkillResult(success=True, output="fallback result")

    monkeypatch.setattr("app.core.llm.LLMClient", FakeLLM)
    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    monkeypatch.setattr(atomic_mod, "execute_tool_call", fake_execute)
    node = TaskNode(
        id="atomic",
        name="读取文档",
        agent="atomic_step",
        retries=1,
        params={
            "instruction": "读取失败时写脚本解析",
            "preferred_tool": "office_doc_read",
            "fallback_tools": ["python_exec"],
        },
    )

    result = asyncio.run(
        AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1"))
    )

    assert result["success"] is True
    assert result["tool"] == "python_exec"
    assert selected["tool"] == "python_exec"


def test_atomic_document_step_executes_planned_tool_without_extra_llm(monkeypatch):
    import app.agents.roles.atomic as atomic_mod

    calls = []

    class ForbiddenLLM:
        def __init__(self):
            raise AssertionError("确定性文档步骤不应实例化额外 LLM")

    async def fake_tools(scene, user_role):
        return [{"type": "function", "function": {"name": "office_doc_read", "parameters": {}}}]

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        calls.append(call)
        return SkillResult(success=True, output="CSV 表格正文")

    monkeypatch.setattr("app.core.llm.LLMClient", ForbiddenLLM)
    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    monkeypatch.setattr(atomic_mod, "execute_tool_call", fake_execute)
    node = TaskNode(
        id="read",
        name="读取 CSV",
        agent="atomic_step",
        params={
            "instruction": "读取表格",
            "preferred_tool": "office_doc_read",
            "inputs": {"doc_id": "d1", "mode": "read"},
        },
    )

    result = asyncio.run(AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1")))

    assert result["success"] is True
    assert result["content"] == "CSV 表格正文"
    assert '"doc_id": "d1"' in calls[0]["function"]["arguments"]


def test_execute_dag_dependency_failure_skips_children():
    worker = FakeWorker("w1", fail_times=99)
    nodes = [_node("t1", agent="w1"), _node("t2", agent="w1", deps=["t1"])]
    store, job = _make_store_job(nodes)
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert final.nodes[0].status == TaskStatus.FAILED
    assert final.nodes[1].status == TaskStatus.SKIPPED


def test_execute_dag_cancel_stops_running_and_pending():
    blocking = asyncio.Event()

    class BlockWorker:
        name = "block"

        async def execute(self, node, ctx):
            await blocking.wait()  # 一直阻塞直到被取消
            return {"success": True, "content": "never"}

    nodes = [_node("t1", agent="block"), _node("t2", agent="w1", deps=["t1"])]
    store, job = _make_store_job(nodes)

    async def scenario():
        task = asyncio.create_task(
            execute_dag(job, {"block": BlockWorker(), "w1": FakeWorker()}, NoopReviewer(), store)
        )
        await asyncio.sleep(0.3)  # 让 t1 进入运行
        job2 = await store.get_job("j1")
        job2.status = JobStatus.CANCELLED
        await store.save_job(job2)
        await asyncio.wait_for(task, timeout=5)
        return await store.get_job("j1")

    final = asyncio.run(scenario())
    assert final.status == JobStatus.CANCELLED
    assert final.nodes[0].status == TaskStatus.INTERRUPTED  # 运行中被终止
    assert final.nodes[1].status == TaskStatus.CANCELLED    # 未执行被取消


def test_orchestrator_submit_and_cancel():
    class FakePlanner(Planner):
        async def plan(
            self,
            user_id,
            request,
            scene="office",
            project_id=None,
            project_ids=None,
            llm_api_key=None,
            clarification_answer=None,
            office_docs=None,
            prior_summaries="",
        ):
            return TaskTree(nodes=[_node("t1", agent="w1")])

    worker = FakeWorker("w1", delay=10)  # 慢任务，便于测试取消
    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job("u1", "测试请求")
        assert job.job_id
        # 等节点进入 RUNNING（后台任务启动 + worker 开始执行）
        await asyncio.sleep(0.3)
        await orch.cancel_job(job.job_id)
        # 等待编排器收尾（轮询检测 CANCELLED → 中断运行节点，约 1s）
        final = None
        for _ in range(30):
            final = await orch.get_job(job.job_id)
            if final and final.status == JobStatus.CANCELLED and final.nodes[0].status == TaskStatus.INTERRUPTED:
                break
            await asyncio.sleep(0.2)
        assert final.status == JobStatus.CANCELLED
        assert final.nodes[0].status == TaskStatus.INTERRUPTED
        return final

    final = asyncio.run(scenario())
    assert final.status == JobStatus.CANCELLED


def test_retrieval_worker_registered():
    assert "retrieval" in WORKERS
    assert WORKERS["retrieval"].skills == ["query_knowledge"]
    names = {w.name for w in list_workers()}
    assert "retrieval" in names


def test_orchestrator_serializes_all_planned_steps():
    nodes = [_node("a"), _node("b"), _node("c", deps=["a"])]

    AgentOrchestrator._serialize_steps(nodes)

    assert [node.id for node in nodes] == ["a", "b", "c"]
    assert nodes[0].depends_on == []
    assert nodes[1].depends_on == ["a"]
    assert nodes[2].depends_on == ["b"]


def test_orchestrator_threads_byok_key_to_worker():
    """BYOK：提交任务携带的临时 key 应通过 WorkerContext 传给 worker，任务结束后释放."""
    seen = {}

    class KeyWorker:
        name = "keyw"

        async def execute(self, node, ctx):
            seen["key"] = ctx.llm_api_key
            return {"success": True, "content": "ok"}

    class FakePlanner(Planner):
        async def plan(
            self,
            user_id,
            request,
            scene="office",
            project_id=None,
            project_ids=None,
            llm_api_key=None,
            clarification_answer=None,
            office_docs=None,
            prior_summaries="",
        ):
            return TaskTree(nodes=[_node("t1", agent="keyw")])

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"keyw": KeyWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job("u1", "测试", llm_api_key="sk-test")
        for _ in range(30):
            await asyncio.sleep(0.2)
            cur = await orch.get_job(job.job_id)
            if cur.status.value in ("completed", "failed", "cancelled", "interrupted"):
                break
        assert orch._job_api_keys == {}  # 任务结束，key 已释放
        return cur

    final = asyncio.run(scenario())
    assert final.status == JobStatus.COMPLETED
    assert seen.get("key") == "sk-test"


def test_orchestrator_rejects_second_active_job_in_same_conversation():
    gate = asyncio.Event()

    class SlowWorker:
        async def execute(self, node, ctx):
            await gate.wait()
            return {"success": True, "content": "ok"}

    class FakePlanner(Planner):
        async def plan(
            self,
            user_id,
            request,
            scene="office",
            project_id=None,
            project_ids=None,
            llm_api_key=None,
            clarification_answer=None,
            office_docs=None,
            prior_summaries="",
        ):
            return TaskTree(nodes=[_node("t1", agent="slow")])

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"slow": SlowWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        first = await orch.submit_job(
            "u1", "总结文档", conversation_id="c1", office_docs=[{"doc_id": "d1"}]
        )
        with pytest.raises(ActiveConversationJobError):
            await orch.submit_job(
                "u1", "总结文档", conversation_id="c1", office_docs=[{"doc_id": "d2"}]
            )
        gate.set()
        await asyncio.gather(*orch._tasks.values())

    asyncio.run(scenario())


def test_orchestrator_limits_user_to_two_active_jobs():
    gate = asyncio.Event()

    class SlowWorker:
        async def execute(self, node, ctx):
            await gate.wait()
            return {"success": True, "content": "ok"}

    class FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node(f"t-{kwargs.get('prior_summaries', '') or 'x'}", agent="slow")])

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"slow": SlowWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        await orch.submit_job("u1", "任务一", conversation_id="c1")
        await orch.submit_job("u1", "任务二", conversation_id="c2")
        with pytest.raises(UserJobLimitError):
            await orch.submit_job("u1", "任务三", conversation_id="c3")
        gate.set()
        await asyncio.gather(*orch._tasks.values())

    asyncio.run(scenario())


def test_orchestrator_completed_job_does_not_count_toward_limit():
    store = InMemoryStateStore()
    completed = Job(
        job_id="done",
        user_id="u1",
        request="done",
        conversation_id="old",
        status=JobStatus.COMPLETED,
    )

    class FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node("t1", agent="w1")])

    orch = AgentOrchestrator(
        store=store,
        planner=FakePlanner(),
        workers={"w1": FakeWorker("w1")},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        await store.create_job(completed)
        one = await orch.submit_job("u1", "任务一", conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        return one

    assert asyncio.run(scenario()).conversation_id == "c1"


def test_temporal_unavailable_is_negative_cached(monkeypatch):
    orch = AgentOrchestrator(temporal_enabled=True)
    calls = 0

    async def unavailable():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    monkeypatch.setattr(
        "app.agents.orchestration.temporal.client.get_temporal_client", unavailable
    )

    async def scenario():
        assert await orch._probe_temporal() is False
        assert await orch._probe_temporal() is False

    asyncio.run(scenario())
    assert calls == 1


def test_single_step_job_reuses_step_output_without_final_llm(monkeypatch):
    class OneStepPlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node("only", agent="w1")])

    class OneWorker:
        async def execute(self, node, ctx):
            return {"success": True, "content": "直接结果"}

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=OneStepPlanner(),
        workers={"w1": OneWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def should_not_summarize(*args, **kwargs):
        raise AssertionError("单步骤任务不应调用最终汇总模型")

    monkeypatch.setattr(orch, "_record_office_summary", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(
        "app.agents.orchestration.temporal.activities.synthesize_final_answer_activity",
        should_not_summarize,
    )

    async def scenario():
        job = await orch.submit_job("u1", "单步骤", conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        return await orch.get_job(job.job_id)

    result = asyncio.run(scenario())
    assert result.result["final_answer"] == "直接结果"


def test_state_store_cas_preserves_cancelled_status():
    store, job = _make_store_job([_node("t1")])

    async def scenario():
        await store.create_job(job)
        worker_snapshot = await store.get_job("j1")
        api_snapshot = await store.get_job("j1")
        api_snapshot.status = JobStatus.CANCELLED
        api_snapshot.nodes[0].status = TaskStatus.CANCELLED
        await store.save_job(api_snapshot)
        worker_snapshot.nodes[0].status = TaskStatus.COMPLETED
        worker_snapshot.nodes[0].result = {"content": "late result"}
        await store.save_job(worker_snapshot)
        return await store.get_job("j1")

    final = asyncio.run(scenario())
    assert final.status == JobStatus.CANCELLED
    assert final.nodes[0].status == TaskStatus.CANCELLED


def test_dependency_context_drops_secrets_and_limits_fields():
    from app.agents.orchestration.context import sanitize_dependency_result

    cleaned = sanitize_dependency_result(
        {
            "content": "ok",
            "api_key": "secret",
            "metadata": {"token": "hidden", "path": "a.txt"},
            "internal_debug": "must drop",
        }
    )
    assert cleaned["content"] == "ok"
    assert "api_key" not in cleaned
    assert "internal_debug" not in cleaned
    assert "token" not in cleaned["metadata"]
