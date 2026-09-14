"""任务级执行引擎契约测试。"""

import asyncio

from lumi_execution import NodeExecutionResult, TaskExecutionEngine
from lumi_orch import JobSpec, NodeSpec


class Executor:
    def __init__(self, failed=()):
        self.failed = set(failed)
        self.calls = []

    async def execute_node(self, _spec, node, dependency_results):
        self.calls.append((node.id, dict(dependency_results)))
        if node.id in self.failed:
            return NodeExecutionResult(
                node_id=node.id, status="failed", error="boom", error_code="TEST_FAILURE"
            )
        return NodeExecutionResult(
            node_id=node.id, status="completed", result={"value": node.id}
        )


def _spec(nodes):
    return JobSpec(job_id="job-1", user_id="user-1", nodes=tuple(nodes))


def test_task_engine_executes_dependencies_and_returns_complete_result():
    executor = Executor()
    spec = _spec([
        NodeSpec(id="a", agent="test"),
        NodeSpec(id="b", agent="test", depends_on=("a",)),
        NodeSpec(id="c", agent="test", depends_on=("a",)),
    ])
    outcome = asyncio.run(TaskExecutionEngine(executor=executor, concurrency=2).run(spec))
    assert outcome.status == "completed"
    assert [item.node_id for item in outcome.node_results] == ["a", "b", "c"]
    assert executor.calls[1][1] == {"a": {"value": "a"}}
    assert outcome.result == {
        "outputs": {
            "a": {"value": "a"},
            "b": {"value": "b"},
            "c": {"value": "c"},
        },
        "completed_node_ids": ["a", "b", "c"],
        "failed_node_ids": [],
    }


def test_failed_dependency_isolated_and_independent_branch_still_finishes():
    executor = Executor(failed={"a"})
    spec = _spec([
        NodeSpec(id="a", agent="test"),
        NodeSpec(id="b", agent="test", depends_on=("a",)),
        NodeSpec(id="c", agent="test"),
    ])
    outcome = asyncio.run(TaskExecutionEngine(executor=executor, concurrency=2).run(spec))
    by_id = {item.node_id: item for item in outcome.node_results}
    assert outcome.status == "failed"
    assert by_id["a"].status == "failed"
    assert by_id["b"].status == "skipped"
    assert by_id["c"].status == "completed"


def test_prior_terminal_results_are_not_executed_again():
    executor = Executor()
    spec = _spec([NodeSpec(id="a", agent="test")])
    prior = (NodeExecutionResult(node_id="a", status="completed", result={"value": "old"}),)
    outcome = asyncio.run(TaskExecutionEngine(executor=executor).run(spec, prior_results=prior))
    assert outcome.status == "completed"
    assert executor.calls == []


def test_aggregation_node_receives_failed_dependency_evidence():
    executor = Executor(failed={"a"})
    spec = _spec([
        NodeSpec(id="a", agent="test"),
        NodeSpec(id="b", agent="test"),
        NodeSpec(id="summary", agent="direct_llm", depends_on=("a", "b"), metadata={"aggregation_node": True}),
    ])
    outcome = asyncio.run(TaskExecutionEngine(executor=executor, concurrency=3).run(spec))
    summary_call = next(call for call in executor.calls if call[0] == "summary")
    assert summary_call[1]["a"]["status"] == "failed"
    assert summary_call[1]["a"]["error_code"] == "TEST_FAILURE"
    assert outcome.status == "failed"
