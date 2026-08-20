"""Measure local DAG scheduling overhead without model, tool, or network I/O.

Run with:
    uv run python scripts/benchmark_orchestration.py

The long task-manifest path intentionally serializes entries in ten-node
windows.  This script measures that real path separately from a monolithic DAG
to keep their performance characteristics distinct.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from loguru import logger

from app.agents.orchestration.dag import execute_dag
from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore


class NoopWorker:
    async def execute(self, node: TaskNode, _ctx) -> dict:
        return {"success": True, "content": node.id}


async def measure_chain(size: int) -> float:
    nodes = [
        TaskNode(
            id=f"step-{index}",
            name=f"step-{index}",
            agent="noop",
            depends_on=[f"step-{index - 1}"] if index else [],
            # The manifest has this same scheduling condition: serial order,
            # but an independently failed entry does not stop later entries.
            metadata={"continue_on_dependency_failure": True},
        )
        for index in range(size)
    ]
    job = Job(
        job_id=str(uuid4()),
        user_id="benchmark",
        request="benchmark",
        nodes=nodes,
        status=JobStatus.RUNNING,
    )
    started = time.perf_counter()
    await execute_dag(
        job,
        {"noop": NoopWorker()},
        NoopReviewer(),
        InMemoryStateStore(),
        concurrency=1,
    )
    return time.perf_counter() - started


async def measure_rolling_manifest(total: int, *, batch_size: int = 10) -> float:
    """Approximate a manifest execution window with the same 10-node DAG size."""
    started = time.perf_counter()
    worker = NoopWorker()
    for offset in range(0, total, batch_size):
        count = min(batch_size, total - offset)
        nodes = [
            TaskNode(
                id=f"step-{offset + index}",
                name=f"step-{offset + index}",
                agent="noop",
                depends_on=[f"step-{offset + index - 1}"] if index else [],
                metadata={"continue_on_dependency_failure": True},
            )
            for index in range(count)
        ]
        job = Job(
            job_id=str(uuid4()),
            user_id="benchmark",
            request="benchmark",
            nodes=nodes,
            status=JobStatus.RUNNING,
        )
        await execute_dag(
            job,
            {"noop": worker},
            NoopReviewer(),
            InMemoryStateStore(),
            concurrency=1,
        )
    return time.perf_counter() - started


async def main() -> None:
    # Node-level timing logs would otherwise dominate both console output and
    # the benchmark itself; normal service logging remains unchanged.
    logger.disable("app")
    print("rolling manifest, 10-node windows (in-memory; no LLM/tool/network I/O)")
    for total in (10, 100, 500):
        elapsed = await measure_rolling_manifest(total)
        print(f"{total:>3} items: {elapsed * 1000:>8.1f} ms  ({elapsed * 1000 / total:>6.2f} ms/item)")

    elapsed = await measure_chain(100)
    print(f"100-node monolithic DAG: {elapsed * 1000:>8.1f} ms  ({elapsed * 10:>6.2f} ms/node)")


if __name__ == "__main__":
    asyncio.run(main())
