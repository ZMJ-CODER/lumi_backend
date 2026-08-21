"""Execution fork/replay tests: immutable history, opaque result refs, and redacted spans."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.orchestration.execution_lineage import (
    list_node_spans,
    persist_result_ref,
    record_node_span,
    resolve_result_ref,
)
from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.state import InMemoryStateStore


class _RecordingWorker(WorkerAgent):
    name = "test_worker"
    description = "test worker"
    skills: list[str] = []

    def __init__(self):
        self.dependency_results: dict = {}
        self.instructions: list[str] = []

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        self.dependency_results = dict((node.metadata or {}).get("dependency_results") or {})
        self.instructions.append(str(node.params.get("instruction") or ""))
        return {"success": True, "content": "branched result"}


def _node(node_id: str, *, depends_on: list[str] | None = None, result: dict | None = None) -> TaskNode:
    return TaskNode(
        id=node_id,
        name=node_id,
        agent="test_worker",
        params={"instruction": f"original {node_id}"},
        depends_on=depends_on or [],
        status=TaskStatus.COMPLETED,
        result=result or {"success": True, "content": f"result {node_id}"},
    )


def test_result_reference_is_owner_scoped_and_hash_verified():
    async def scenario():
        result = {
            "success": True,
            "content": "allowed content",
            "api_key": "must not be stored",
            "metadata": {"token": "must not be stored", "safe": "yes"},
        }
        ref = await persist_result_ref("lineage-owner", result)
        assert ref and set(ref) == {"id", "sha256"}
        own = await resolve_result_ref("lineage-owner", ref)
        assert own == {"success": True, "content": "allowed content", "metadata": {"safe": "yes"}}
        assert await resolve_result_ref("different-owner", ref) is None
        assert await resolve_result_ref("lineage-owner", {**ref, "sha256": "0" * 64}) is None

    asyncio.run(scenario())


def test_node_spans_are_redacted_lifecycle_metadata_only():
    async def scenario():
        node = _node("span-node")
        node.params = {"instruction": "private prompt body", "api_key": "not visible"}
        node.result = {"content": "private model output", "tool": "safe_tool"}
        await record_node_span(execution_id="span-execution", job_id="span-job", node=node, event="finished")
        spans = await list_node_spans("span-execution")
        assert spans
        span = spans[-1]
        assert span["node_id"] == "span-node"
        assert span["tool"] == "safe_tool"
        serialized = str(span)
        assert "private prompt body" not in serialized
        assert "private model output" not in serialized
        assert "not visible" not in serialized
        assert "api_key" not in span

    asyncio.run(scenario())


def test_fork_reuses_body_free_prefix_and_reruns_selected_node():
    async def scenario():
        store = InMemoryStateStore()
        worker = _RecordingWorker()
        source = Job(
            job_id="source-execution",
            execution_id="source-execution",
            root_execution_id="source-execution",
            user_id="fork-owner",
            request="complete task",
            status=JobStatus.COMPLETED,
            nodes=[
                _node("read", result={"success": True, "content": "upstream evidence", "token": "discard"}),
                _node("write", depends_on=["read"]),
            ],
        )
        await store.create_job(source)
        orchestrator = AgentOrchestrator(
            store=store,
            workers={"test_worker": worker},
            temporal_enabled=False,
        )

        branch = await orchestrator.fork_job(
            source.job_id,
            node_id="write",
            instruction="use a different style",
        )
        await orchestrator._tasks[branch.job_id]
        finished = await store.get_job(branch.job_id)
        original = await store.get_job(source.job_id)

        assert finished and finished.status == JobStatus.COMPLETED
        assert finished.execution_id == finished.job_id
        assert finished.parent_execution_id == source.execution_id
        assert finished.root_execution_id == source.execution_id
        prefix, rerun = finished.nodes
        assert prefix.status == TaskStatus.COMPLETED
        assert prefix.result is None
        assert prefix.metadata["replay_prefix"] is True
        assert set(prefix.metadata["result_ref"]) == {"id", "sha256"}
        assert rerun.status == TaskStatus.COMPLETED
        assert worker.instructions == ["use a different style"]
        assert worker.dependency_results["read"]["content"] == "upstream evidence"
        assert "token" not in worker.dependency_results["read"]
        assert original and original.nodes[0].result["content"] == "upstream evidence"
        assert original.nodes[1].params["instruction"] == "original write"

    asyncio.run(scenario())


def test_fork_rejects_crossing_committed_prefix_effect():
    async def scenario():
        store = InMemoryStateStore()
        source = Job(
            job_id="effect-source",
            user_id="effect-owner",
            request="task",
            status=JobStatus.COMPLETED,
            nodes=[
                TaskNode(
                    id="send",
                    name="send email",
                    agent="test_worker",
                    params={"instruction": "send"},
                    status=TaskStatus.COMPLETED,
                    result={"success": True, "content": "sent"},
                    effect_status="committed",
                ),
                _node("follow-up", depends_on=["send"]),
            ],
        )
        await store.create_job(source)
        orchestrator = AgentOrchestrator(
            store=store,
            workers={"test_worker": _RecordingWorker()},
            temporal_enabled=False,
        )
        with pytest.raises(RuntimeError, match="副作用"):
            await orchestrator.fork_job(source.job_id, node_id="follow-up")

    asyncio.run(scenario())


def test_fork_rejects_manifest_execution():
    async def scenario():
        store = InMemoryStateStore()
        source = Job(
            job_id="manifest-source",
            user_id="manifest-owner",
            request="task",
            status=JobStatus.COMPLETED,
            routing={"runtime": "manifest_temporal"},
            nodes=[_node("only")],
        )
        await store.create_job(source)
        orchestrator = AgentOrchestrator(
            store=store,
            workers={"test_worker": _RecordingWorker()},
            temporal_enabled=False,
        )
        with pytest.raises(RuntimeError, match="滚动清单"):
            await orchestrator.fork_job(source.job_id, node_id="only")

    asyncio.run(scenario())
