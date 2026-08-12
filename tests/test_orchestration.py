"""多智能体编排框架测试：DAG 校验 / 拓扑执行 / 重试 / 取消 / 编排器."""

import asyncio

import pytest

from app.agents.orchestration.dag import DagValidationError, execute_dag, validate_dag
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planner import Planner, TaskTree
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore
from app.agents.orchestration.workers import WORKERS, WorkerContext, list_workers


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
        async def plan(self, user_id, request, scene="office", project_id=None, llm_api_key=None, clarification_answer=None):
            return TaskTree(nodes=[_node("t1", agent="w1")])

    worker = FakeWorker("w1", delay=10)  # 慢任务，便于测试取消
    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"w1": worker},
        review=NoopReviewer(),
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


def test_orchestrator_threads_byok_key_to_worker():
    """BYOK：提交任务携带的临时 key 应通过 WorkerContext 传给 worker，任务结束后释放."""
    seen = {}

    class KeyWorker:
        name = "keyw"

        async def execute(self, node, ctx):
            seen["key"] = ctx.llm_api_key
            return {"success": True, "content": "ok"}

    class FakePlanner(Planner):
        async def plan(self, user_id, request, scene="office", project_id=None, llm_api_key=None, clarification_answer=None):
            return TaskTree(nodes=[_node("t1", agent="keyw")])

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"keyw": KeyWorker()},
        review=NoopReviewer(),
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
