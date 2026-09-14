"""Application adapter contract for the task-level execution engine."""

import asyncio

from lumi_execution import NodeExecutionResult

from app.agents.orchestration.execution.node import ApplicationTaskNodeExecutor
from app.agents.orchestration.execution.service import ApplicationTaskExecutionService
from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.runtime.state import InMemoryStateStore


def test_application_task_service_returns_completed_outputs(monkeypatch):
    async def execute_node(_self, _spec, node, dependency_results):
        return NodeExecutionResult(
            node_id=node.id,
            status="completed",
            result={"node": node.id, "dependencies": dict(dependency_results)},
        )

    monkeypatch.setattr(ApplicationTaskNodeExecutor, "execute_node", execute_node)
    store = InMemoryStateStore()
    job = Job(
        job_id="execution-adapter-job",
        user_id="user-1",
        request="生成并汇总结果",
        nodes=[
            TaskNode(id="first", agent="test"),
            TaskNode(id="second", agent="test", depends_on=["first"]),
        ],
    )

    outcome = asyncio.run(
        ApplicationTaskExecutionService(store=store, workers={}, review=object()).execute(
            job, concurrency=2
        )
    )

    saved = asyncio.run(store.get_job(job.job_id))
    assert outcome.status == "completed"
    assert outcome.result == {
        "outputs": {
            "first": {"node": "first", "dependencies": {}},
            "second": {"node": "second", "dependencies": {"first": {"node": "first", "dependencies": {}}}},
        },
        "completed_node_ids": ["first", "second"],
        "failed_node_ids": [],
    }
    assert saved is not None
    assert saved.status == JobStatus.COMPLETED
    assert saved.result == outcome.result
