"""多智能体编排框架测试：DAG 校验 / 拓扑执行 / 重试 / 取消 / 编排器."""

import asyncio
import importlib
from contextlib import asynccontextmanager

import pytest

from app.agents.orchestration.execution.validation import DagValidationError, execute_dag, validate_dag
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import (
    AgentBackpressureError,
    AgentOrchestrator,
    UserJobLimitError,
)
from app.agents.orchestration import admission
from app.agents.orchestration.planner import Planner, TaskTree
from app.agents.orchestration.planning.compilation import PlanCompilationService
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore

from app.agents.orchestration.workers import WORKERS, WorkerContext, list_workers
from app.agents.roles.atomic import AtomicStepAgent
from app.core.config import settings


@pytest.fixture(autouse=True)
def _effect_journal_test_double():
    """DAG unit tests do not require a live Postgres journal."""
    from app.agents.orchestration.effects import set_effect_journal_repository_for_tests
    from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository

    set_effect_journal_repository_for_tests(InMemoryEffectJournalRepository())
    yield
    set_effect_journal_repository_for_tests(None)


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


def test_resource_lock_renews_redis_lease_while_held(monkeypatch):
    from app.agents.orchestration import resources

    calls = []

    class FakeRedis:
        async def eval(self, script, _keys, *_args):
            calls.append(script)
            return 1

    coordinator = resources.ResourceCoordinator()

    async def fake_redis():
        return FakeRedis()

    monkeypatch.setattr(coordinator, "_redis", fake_redis)

    async def scenario():
        async with coordinator.claim([ResourceClaim(key="doc:1", mode="write")], ttl=3):
            await asyncio.sleep(1.1)

    asyncio.run(scenario())
    assert resources._ACQUIRE_SCRIPT in calls
    assert resources._RENEW_SCRIPT in calls
    assert resources._RELEASE_SCRIPT in calls


def test_write_resource_waits_without_worker_or_channel_then_recovers(monkeypatch):
    """Write coordination is fail-closed, but recovery reuses the same node."""
    from app.agents.orchestration import resources

    class RecoveringCoordinator:
        def __init__(self):
            self.checks = 0

        async def write_coordination_available(self, _claims):
            self.checks += 1
            return self.checks >= 2

        @asynccontextmanager
        async def claim(self, _claims, ttl=360):
            yield

    worker = FakeWorker("write-worker")
    store, job = _make_store_job([
        _node(
            "write",
            agent="worker",
            resource_claims=[ResourceClaim(key="document:1", mode="write")],
        )
    ])
    monkeypatch.setattr(resources, "resource_coordinator", RecoveringCoordinator())

    asyncio.run(execute_dag(job, {"worker": worker}, NoopReviewer(), store, concurrency=1))
    saved = asyncio.run(store.get_job(job.job_id))
    assert worker.calls == 1
    assert saved.status == JobStatus.COMPLETED
    assert saved.nodes[0].status == TaskStatus.COMPLETED


def test_admission_local_lease_can_only_renew_existing_job(monkeypatch):
    from app.agents.orchestration.admission import JobAdmission
    import app.core.redis as redis_module

    admission_control = JobAdmission()
    now = __import__("time").time()
    admission_control._global["job-1"] = now + 60
    admission_control._users["u1"] = {"job-1": now + 60}

    def unavailable_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(redis_module, "get_redis", unavailable_redis)
    assert asyncio.run(admission_control.renew("job-1", "u1")) is True
    assert asyncio.run(admission_control.renew("missing", "u1")) is False


def test_admission_adapter_reads_runtime_limits(monkeypatch):
    from app.agents.orchestration.admission import JobAdmission

    monkeypatch.setattr(settings, "AGENT_SUBMISSION_MAX_INFLIGHT", 3)
    admission_control = JobAdmission()

    assert admission_control.limits().max_inflight == 3


def test_job_control_ownership_is_checked_before_mutation(monkeypatch):
    from app.api.v1 import agents as agents_api
    from app.core.exceptions import NotFoundException

    async def get_job(_job_id):
        return Job(job_id="owned", user_id="owner", request="test")

    monkeypatch.setattr(agents_api.orchestrator, "get_job", get_job)
    owned = asyncio.run(agents_api._get_owned_job("owned", "owner"))
    assert owned.user_id == "owner"
    with pytest.raises(NotFoundException):
        asyncio.run(agents_api._get_owned_job("owned", "other-user"))


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


def test_execute_dag_mixes_independent_and_dependent_tasks():
    """无关任务可并行，依赖任务只在其前置任务成功后运行。"""
    events = []

    class RecordingWorker:
        async def execute(self, node, _ctx):
            events.append((node.id, "start"))
            if node.id == "prepare":
                await asyncio.sleep(0.02)
            events.append((node.id, "end"))
            return {"success": True, "content": node.id}

    nodes = [
        _node("prepare", agent="worker"),
        _node("independent", agent="worker"),
        _node("process", agent="worker", deps=["prepare"]),
        _node("publish", agent="worker", deps=["process"]),
    ]
    store, job = _make_store_job(nodes)
    asyncio.run(
        execute_dag(job, {"worker": RecordingWorker()}, NoopReviewer(), store, concurrency=2)
    )

    final = asyncio.run(store.get_job("j1"))
    positions = {event: index for index, event in enumerate(events)}
    assert final.status == JobStatus.COMPLETED
    assert all(node.status == TaskStatus.COMPLETED for node in final.nodes)
    assert positions[("process", "start")] > positions[("prepare", "end")]
    assert positions[("publish", "start")] > positions[("process", "end")]
    # independent 与 prepare 可同时进入调度，不需要等待 prepare 完成。
    assert positions[("independent", "start")] < positions[("prepare", "end")]


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


def test_execute_dag_does_not_retry_invalid_args():
    """参数问题应要求重规划，不能机械重复调用同一工具。"""
    worker = FakeWorker(
        "w1",
        result={"success": False, "error": "缺少 doc_id", "error_code": "INVALID_ARGS"},
    )
    store, job = _make_store_job([_node("t1", agent="w1", max_retries=2)])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert worker.calls == 1
    assert final.nodes[0].metadata["recovery"]["category"] == "input"
    assert final.nodes[0].metadata["recovery"]["replan_required"] is True


def test_execute_dag_retries_explicit_transient_failure():
    worker = FakeWorker(
        "w1",
        result={"success": False, "error": "network timeout", "error_code": "TIMEOUT", "retryable": True},
    )
    store, job = _make_store_job([_node("t1", agent="w1", max_retries=2)])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    assert worker.calls == 3


def test_langgraph_runner_switches_fallback_only_for_alternative_failure():
    """LangGraph 恢复节点应自动切换工具，不进入用户审批流程。"""
    class FallbackWorker:
        calls = 0

        async def execute(self, node, ctx):
            self.calls += 1
            if self.calls == 1:
                return {
                    "success": False,
                    "error": "解析器无法读取此格式",
                    "error_code": "EXEC_ERROR",
                    "use_next_tool": True,
                }
            return {"success": True, "content": "备用读取成功"}

    node = _node("t1", agent="w1", max_retries=1)
    node.metadata["tool_index"] = 0
    worker = FallbackWorker()
    store, job = _make_store_job([node])
    asyncio.run(execute_dag(job, {"w1": worker}, NoopReviewer(), store))
    final = asyncio.run(store.get_job("j1"))
    assert worker.calls == 2
    assert final.nodes[0].status == TaskStatus.COMPLETED
    assert final.nodes[0].metadata["tool_index"] == 1


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
            {"type": "function", "function": {"name": "office_doc_read", "parameters": {
                "type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"],
            }}},
            {"type": "function", "function": {"name": "python_exec", "parameters": {
                "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"],
            }}},
        ]

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        return SkillResult(success=True, output="fallback result")

    async def fake_extract(**kwargs):
        selected["tool"] = kwargs["tool_definition"]["function"]["name"]
        return {"code": "print('fallback')"}

    monkeypatch.setattr("app.agents.langchain.agent.extract_tool_arguments", fake_extract)
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


def test_atomic_step_marks_capability_failure_for_alternative(monkeypatch):
    import app.agents.roles.atomic as atomic_mod

    async def fake_tools(scene, user_role):
        return [{"type": "function", "function": {"name": "office_doc_read", "parameters": {
            "type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"],
        }}}]

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        return SkillResult(success=False, error="需要隔离沙箱", error_code="SANDBOX_REQUIRED")

    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    monkeypatch.setattr(atomic_mod, "execute_tool_call", fake_execute)
    node = TaskNode(
        id="atomic", name="处理文档", agent="atomic_step",
        params={"instruction": "处理文档", "preferred_tool": "office_doc_read", "fallback_tools": ["python_exec"], "inputs": {"doc_id": "d1"}},
    )
    result = asyncio.run(AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1")))
    assert result["success"] is False
    assert result["use_next_tool"] is True
    assert result["retryable"] is True


def test_atomic_step_refuses_missing_noninferable_planning_input(monkeypatch):
    """历史/异常计划也必须在执行入口得到澄清，而不是让模型猜 doc_id。"""
    import app.agents.roles.atomic as atomic_mod
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    class EditTool(Tool):
        name = "test_edit_requires_doc"
        description = "测试编辑工具。适用：已明确文档；禁用：文档目标未知。"
        scenes = ["office"]
        plan_required_fields = ["doc_id"]
        parameters_schema = {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}, "instruction": {"type": "string"}},
            "required": ["doc_id", "instruction"],
        }

        async def execute(self, params, context=None) -> ToolOutput:
            raise AssertionError("缺少 doc_id 的节点不能触发工具执行")

    ToolRegistry.register(EditTool())

    async def fake_tools(*_args):
        return [{"type": "function", "function": {"name": "test_edit_requires_doc", "parameters": EditTool.parameters_schema}}]

    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    node = TaskNode(
        id="edit", name="编辑文档", agent="atomic_step",
        params={"instruction": "修改附件标题", "preferred_tool": "test_edit_requires_doc", "inputs": {}},
    )
    result = asyncio.run(AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1", scene="office")))

    assert result["error_code"] == "MISSING_PARAMETER"
    assert "doc_id" in result["error"]


def test_atomic_document_step_executes_planned_tool_without_extra_llm(monkeypatch):
    import app.agents.roles.atomic as atomic_mod

    calls = []

    class ForbiddenLLM:
        def __init__(self):
            raise AssertionError("确定性文档步骤不应实例化额外 LLM")

    async def fake_tools(scene, user_role):
        return [{"type": "function", "function": {"name": "office_doc_read", "parameters": {
            "type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"],
        }}}]

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


def test_atomic_instruction_tool_executes_without_function_calling(monkeypatch):
    """Writing skills expose ``instruction`` as their compact direct contract."""
    import app.agents.roles.atomic as atomic_mod

    calls = []

    async def fake_tools(scene, user_role):
        return [{
            "type": "function",
            "function": {
                "name": "compose_official_doc",
                "parameters": {"type": "object", "properties": {"instruction": {"type": "string"}}},
            },
        }]

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        calls.append(call)
        return SkillResult(success=True, output="演讲稿正文")

    async def forbidden_tool_choice(**kwargs):
        raise AssertionError("完整 instruction 的写作步骤不应强制 Function Calling")

    class DirectWritingContract:
        direct_instruction_field = "instruction"
        direct_required_fields = ["instruction"]
        direct_input_aliases = {}

    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    monkeypatch.setattr(atomic_mod, "execute_tool_call", fake_execute)
    monkeypatch.setattr("app.agents.langchain.agent.choose_single_tool", forbidden_tool_choice)
    monkeypatch.setattr(
        "app.agents.skills.registry.ToolRegistry.get",
        lambda name: DirectWritingContract() if name == "compose_official_doc" else None,
    )
    node = TaskNode(
        id="write",
        name="起草演讲稿",
        agent="atomic_step",
        params={"instruction": "帮我写一份普通的演讲稿", "preferred_tool": "compose_official_doc"},
    )

    result = asyncio.run(AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1")))

    assert result["success"] is True
    assert result["content"] == "演讲稿正文"
    assert '"instruction": "帮我写一份普通的演讲稿"' in calls[0]["function"]["arguments"]


def test_atomic_step_extracts_missing_parameters_without_function_calling(monkeypatch):
    import app.agents.roles.atomic as atomic_mod

    calls = []

    async def fake_tools(scene, user_role):
        return [{
            "type": "function",
            "function": {
                "name": "OpenApp",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "args": {"type": "array"}},
                    "required": ["name"],
                },
            },
        }]

    async def fake_extract(**kwargs):
        assert kwargs["tool_definition"]["function"]["name"] == "OpenApp"
        return {"name": "记事本", "args": []}

    async def fake_execute(call, *args, **kwargs):
        from app.agents.skills.base import SkillResult

        calls.append(call)
        return SkillResult(success=True, output="已请求打开应用")

    monkeypatch.setattr(atomic_mod, "get_tools_for_scene", fake_tools)
    monkeypatch.setattr(atomic_mod, "execute_tool_call", fake_execute)
    monkeypatch.setattr("app.agents.langchain.agent.extract_tool_arguments", fake_extract)
    node = TaskNode(
        id="open",
        agent="atomic_step",
        params={"instruction": "请打开记事本", "preferred_tool": "OpenApp"},
    )

    result = asyncio.run(AtomicStepAgent().execute(node, WorkerContext(user_id="u1", job_id="j1")))

    assert result["success"] is True
    assert '"name": "记事本"' in calls[0]["function"]["arguments"]


def test_named_document_selection_refuses_missing_file_instead_of_scanning_all(monkeypatch):
    from app.agents.orchestration.planner import LlmPlanner

    async def forbidden_projects(*args, **kwargs):
        raise AssertionError("未定位文件时不应进入自由规划")

    planner = LlmPlanner()
    monkeypatch.setattr(planner, "_list_projects", forbidden_projects)
    tree = asyncio.run(
        planner.plan(
            "u1",
            "请总结 missing.csv",
            office_docs=[
                {"doc_id": "calendar", "filename": "calendar.ics"},
                {"doc_id": "scores", "filename": "scores.csv"},
            ],
        )
    )

    assert tree.nodes == []
    assert "missing.csv" in (tree.clarification or "")


def test_named_document_selection_keeps_only_explicit_document():
    from app.agents.orchestration.document_scope import select_named_office_documents

    selected, unresolved, referenced = select_named_office_documents(
        "请总结 scores.csv",
        [
            {"doc_id": "calendar", "filename": "calendar.ics"},
            {"doc_id": "scores", "filename": "scores.csv"},
            {"doc_id": "mail", "filename": "mail.eml"},
        ],
    )

    assert referenced is True
    assert unresolved == []
    assert selected == [{"doc_id": "scores", "filename": "scores.csv"}]


def test_named_document_selection_does_not_treat_output_filename_as_input():
    from app.agents.orchestration.document_scope import select_named_office_documents

    selected, unresolved, referenced = select_named_office_documents(
        "基于 scores.csv 生成 report.txt",
        [
            {"doc_id": "scores", "filename": "scores.csv"},
            {"doc_id": "calendar", "filename": "calendar.ics"},
        ],
    )

    assert referenced is True
    assert unresolved == []
    assert selected == [{"doc_id": "scores", "filename": "scores.csv"}]


def test_multilingual_output_filename_is_not_treated_as_input():
    from app.agents.orchestration.document_scope import extract_output_contract, select_named_office_documents

    docs = [{"doc_id": "scores", "filename": "scores.csv"}]
    for request in (
        "基于 scores.csv 存成 review.md",
        "Use scores.csv and save it as review.md",
        "Usa scores.csv y guárdalo como review.md",
        "scores.csv を review.md として保存してください",
    ):
        selected, unresolved, referenced = select_named_office_documents(request, docs)
        assert referenced is True
        assert unresolved == []
        assert selected == docs
        assert extract_output_contract(request)["expected_output_names"] == ["review.md"]


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


def test_oversized_plan_is_rolled_through_logical_frontier():
    """The real submit path must keep a long plan executable in bounded windows."""
    class LongPlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(
                nodes=[_node(f"step-{index}", agent="w1") for index in range(9)],
                plan_text="long plan",
            )

    async def scenario():
        orch = AgentOrchestrator(
            store=InMemoryStateStore(),
            planner=LongPlanner(),
            workers={"w1": FakeWorker("w1", delay=0.05)},
            review=NoopReviewer(),
            temporal_enabled=False,
        )
        job = await orch.submit_job("00000000-0000-4000-8000-000000000001", "处理这组长任务")
        assert len(job.nodes) <= settings.AGENT_LOGICAL_PLAN_FRONTIER_SIZE
        assert job.routing["logical_plan"]["progress"]["total"] == 9
        await orch._tasks[job.job_id]
        final = await orch.get_job(job.job_id)
        assert final is not None
        return final

    final = asyncio.run(scenario())
    assert final.status == JobStatus.COMPLETED
    assert final.routing["logical_plan"]["progress"]["completed"] == 9


def test_parallel_preserved_dag_does_not_enter_logical_frontier():
    """短小且已声明并行语义的 DAG 必须保留完整执行图。"""
    class ParallelPlanner(Planner):
        async def plan(self, *args, **kwargs):
            nodes = [_node(f"parallel-{index}", agent="w1") for index in range(3)]
            for node in nodes:
                node.metadata["preserve_dependencies"] = True
            return TaskTree(nodes=nodes, plan_text="parallel")

    async def scenario():
        orch = AgentOrchestrator(
            store=InMemoryStateStore(),
            planner=ParallelPlanner(),
            workers={"w1": FakeWorker("w1", delay=0.01)},
            review=NoopReviewer(),
            temporal_enabled=False,
        )
        job = await orch.submit_job("parallel-owner", "并行处理三个只读步骤")
        assert len(job.nodes) == 3
        assert "logical_plan" not in job.routing
        # 多结果任务会进入最终交付汇总；该单测只验证物化边界，不依赖真实模型。
        async def deterministic_delivery(_job):
            _job.result = {"final_answer": "parallel done"}
            await orch._store.save_job(_job)

        orch._execution_loop._synthesize_final_answer = deterministic_delivery
        await orch._tasks[job.job_id]
        return await orch.get_job(job.job_id)

    final = asyncio.run(scenario())
    assert final.status == JobStatus.COMPLETED
    assert len(final.nodes) == 3


def test_large_preserved_parallel_dag_is_rolled_into_logical_frontier(monkeypatch):
    """Parallel semantics must survive, but an oversized frontier is batched."""
    class LargeParallelPlanner(Planner):
        async def plan(self, *args, **kwargs):
            nodes = [_node(f"parallel-{index}", agent="w1") for index in range(8)]
            for node in nodes:
                node.metadata["preserve_dependencies"] = True
                node.metadata["planner_generated"] = True
            return TaskTree(nodes=nodes, plan_text="large parallel")

    async def formatter_unavailable(*_args, **_kwargs):
        raise RuntimeError("模型连接异常")

    monkeypatch.setattr(
        "app.agents.orchestration.temporal.activities.synthesize_final_answer_activity",
        formatter_unavailable,
    )

    async def scenario():
        orch = AgentOrchestrator(
            store=InMemoryStateStore(),
            planner=LargeParallelPlanner(),
            workers={"w1": FakeWorker("w1", delay=0.01)},
            review=NoopReviewer(),
            temporal_enabled=False,
        )
        job = await orch.submit_job("large-parallel-owner", "并行处理八个独立只读步骤")
        assert "logical_plan" in job.routing
        assert len(job.nodes) <= settings.AGENT_LOGICAL_PLAN_FRONTIER_SIZE
        await orch._tasks[job.job_id]
        return await orch.get_job(job.job_id)

    final = asyncio.run(scenario())
    assert final.status == JobStatus.COMPLETED
    assert final.result["final_answer"].count("result-w1") == 8
    assert final.result["delivery_status"] == "degraded"


def test_retrieval_worker_registered():
    assert "retrieval" in WORKERS
    assert WORKERS["retrieval"].skills == ["query_knowledge"]
    names = {w.name for w in list_workers()}
    assert "retrieval" in names


def test_orchestrator_serializes_all_planned_steps():
    nodes = [_node("a"), _node("b"), _node("c", deps=["a"])]

    async def unused_plan(_context):
        raise AssertionError("serialization must not invoke the planner")

    compiler = PlanCompilationService(workers={}, plan_with_context=unused_plan)
    compiler.normalize_for_replan(
        nodes,
        "test",
        preserve_dependencies=False,
        adapt_workers=False,
    )

    assert [node.id for node in nodes] == ["a", "b", "c"]
    assert nodes[0].depends_on == []
    assert nodes[1].depends_on == ["a"]
    assert nodes[2].depends_on == ["b"]


def test_l2_missing_prerequisite_escalation_becomes_clarification():
    """节点只上报 L2，编排器将其收敛为澄清而非盲目重试。"""
    class NeedInputWorker:
        async def execute(self, _node, _ctx):
            return {
                "success": False,
                "error": "缺少要处理的合同文件",
                "error_code": "MISSING_PARAMETER",
                "retryable": False,
            }

    store = InMemoryStateStore()
    orch = AgentOrchestrator(
        store=store, workers={"worker": NeedInputWorker()},
        review=NoopReviewer(), temporal_enabled=False,
    )
    job = Job(
        job_id="l2-input", user_id="u1", request="核对合同", scene="office",
        status=JobStatus.RUNNING,
        routing={"level": "m2"},
        nodes=[TaskNode(id="need-file", agent="worker")],
    )

    async def scenario():
        await store.create_job(job)
        await orch._run_job(job.job_id)
        return await store.get_job(job.job_id)

    final = asyncio.run(scenario())
    assert final.status == JobStatus.COMPLETED
    assert final.result["type"] == "clarification"
    assert final.nodes[0].status == TaskStatus.ESCALATED
    assert final.routing["escalations"][0]["reason"] == "missing_prerequisite"


def test_l2_approval_resumes_only_the_approved_node():
    """审批门只重放同一节点，并绑定工具的规范化参数。"""
    from app.agents.skills.executor import tool_call_fingerprint

    seen_confirmations = []
    args = {"file_path": "report.txt"}
    fingerprint = tool_call_fingerprint("Delete", args)

    class ApprovalWorker:
        async def execute(self, _node, ctx):
            seen_confirmations.append(set(ctx.confirmed_tool_calls))
            if fingerprint not in ctx.confirmed_tool_calls:
                return {
                    "success": False,
                    "error": "删除文件需要用户确认",
                    "error_code": "NEEDS_CONFIRMATION",
                    "tool": "Delete",
                    "approval_fingerprint": fingerprint,
                }
            return {"success": True, "content": "文件已删除"}

    store = InMemoryStateStore()
    orch = AgentOrchestrator(
        store=store, workers={"worker": ApprovalWorker()},
        review=NoopReviewer(), temporal_enabled=False,
    )
    job = Job(
        job_id="l2-approval", user_id="u1", request="删除该文件", scene="office",
        status=JobStatus.RUNNING, routing={"level": "m2"},
        nodes=[TaskNode(id="delete", agent="worker", params={"preferred_tool": "Delete"})],
    )

    async def scenario():
        await store.create_job(job)
        await orch._run_job(job.job_id)
        waiting = await store.get_job(job.job_id)
        await orch.approve_job(job.job_id, "delete", True)
        await orch._tasks[job.job_id]
        return waiting, await store.get_job(job.job_id)

    waiting, final = asyncio.run(scenario())
    assert waiting.status == JobStatus.WAITING_APPROVAL
    assert waiting.nodes[0].metadata["approval_tool"] == "Delete"
    assert waiting.nodes[0].metadata["approval_fingerprint"] == fingerprint
    assert final.status == JobStatus.COMPLETED
    assert seen_confirmations == [set(), {fingerprint}]


def test_tool_confirmation_is_bound_to_exact_arguments():
    from app.agents.skills.executor import is_tool_call_confirmed, tool_call_fingerprint

    approved = {tool_call_fingerprint("Delete", {"file_path": "a.txt", "force": False})}
    assert is_tool_call_confirmed("Delete", {"force": False, "file_path": "a.txt"}, approved)
    assert not is_tool_call_confirmed("Delete", {"file_path": "b.txt", "force": False}, approved)
    assert not is_tool_call_confirmed("send_email", {"path": "a.txt", "force": False}, approved)


def test_l3_replan_mounts_subgraph_and_preserves_completed_nodes():
    """L3 不可由 worker 改图：编排器保留成果并挂载规划器子图。"""
    class SubgraphPlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[])

        async def plan_for_level(self, *_args, **_kwargs):
            return TaskTree(nodes=[TaskNode(id="replacement", agent="worker")], plan_text="替代子图")

    store = InMemoryStateStore()
    orch = AgentOrchestrator(
        store=store, planner=SubgraphPlanner(), workers={"worker": FakeWorker("worker")},
        review=NoopReviewer(), temporal_enabled=False,
    )
    job = Job(
        job_id="l3-subgraph", user_id="u1", request="处理资料并核对", scene="office",
        status=JobStatus.FAILED,
        routing={"level": "m2", "upgrade_count": 0, "replan_count": 0},
        nodes=[
            TaskNode(id="done", agent="worker", status=TaskStatus.COMPLETED, result={"content": "已定位资料"}),
            TaskNode(
                id="failed", agent="worker", status=TaskStatus.ESCALATED,
                error="当前方法不足", error_code="CAPABILITY_UNAVAILABLE",
                metadata={"escalation": {"level": "plan", "reason": "capability_gap"}},
            ),
        ],
    )
    orch._job_plan_context[job.job_id] = {
        "user_id": "u1", "request": job.request, "scene": "office", "project_id": None,
        "project_ids": None, "llm_api_key": None, "clarification_answer": None,
        "office_docs": None, "prior_summaries": "",
    }

    async def scenario():
        await store.create_job(job)
        changed = await orch._maybe_replan_failed_job(job, None)
        return changed, await store.get_job(job.job_id)

    changed, saved = asyncio.run(scenario())
    assert changed is True
    assert [node.id for node in saved.nodes] == ["done", "replacement"]
    assert saved.nodes[1].depends_on == ["done"]
    mounted = saved.routing["mounted_subgraphs"][-1]
    assert mounted["anchor_nodes"] == ["done"]
    assert mounted["retired_nodes"] == ["failed"]


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


def test_orchestrator_allows_two_active_jobs_in_same_conversation():
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
        second = await orch.submit_job(
            "u1", "总结文档", conversation_id="c1", office_docs=[{"doc_id": "d2"}]
        )
        assert second.job_id != first.job_id
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


def test_orchestrator_applies_global_admission_backpressure(monkeypatch):
    """不同用户也不能绕过全局办公任务容量。"""
    gate = asyncio.Event()

    class SlowWorker:
        async def execute(self, node, ctx):
            await gate.wait()
            return {"success": True, "content": "ok"}

    class FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node("t1", agent="slow")])

    monkeypatch.setattr(settings, "AGENT_GLOBAL_ACTIVE_JOB_LIMIT", 1)
    admission.job_admission._inflight.clear()
    admission.job_admission._global.clear()
    admission.job_admission._users.clear()
    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=FakePlanner(),
        workers={"slow": SlowWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        await orch.submit_job("u1", "任务一", conversation_id="c1")
        with pytest.raises(AgentBackpressureError):
            await orch.submit_job("u2", "任务二", conversation_id="c2")
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


def test_orchestrator_returns_planning_model_error_without_starting_task():
    class FailedPlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(
                nodes=[],
                error="当前模型账户余额不足，办公任务已停止。请充值，或在模型设置中切换到可用模型后重试。",
                error_code="MODEL_INSUFFICIENT_BALANCE",
            )

    store = InMemoryStateStore()
    orch = AgentOrchestrator(
        store=store,
        planner=FailedPlanner(),
        workers={},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job("u1", "处理文档", conversation_id="c1")
        saved = await store.get_job(job.job_id)
        return job, saved

    job, saved = asyncio.run(scenario())
    assert job.status == JobStatus.FAILED
    assert job.result["type"] == "planning_error"
    assert saved.status == JobStatus.FAILED
    assert orch._tasks == {}


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


def test_multiple_deterministic_tool_nodes_skip_final_llm(monkeypatch):
    """纯计算 DAG 的交付不应重新依赖模型网络。"""
    class MultiStepPlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=[_node("a", agent="w1"), _node("b", agent="w1")])

    class CalculatorWorker:
        async def execute(self, node, ctx):
            return {"success": True, "content": f"{node.id}=ok", "tool": "Calculator"}

    orch = AgentOrchestrator(
        store=InMemoryStateStore(),
        planner=MultiStepPlanner(),
        workers={"w1": CalculatorWorker()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def should_not_summarize(*args, **kwargs):
        raise AssertionError("确定性工具结果不应调用最终汇总模型")

    monkeypatch.setattr(orch, "_record_office_summary", lambda *args, **kwargs: asyncio.sleep(0))
    monkeypatch.setattr(
        "app.agents.orchestration.temporal.activities.synthesize_final_answer_activity",
        should_not_summarize,
    )

    async def scenario():
        job = await orch.submit_job("u1", "多个确定性步骤", conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        return await orch.get_job(job.job_id)

    result = asyncio.run(scenario())
    assert result.result["final_answer"] == "a=ok\nb=ok"


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
