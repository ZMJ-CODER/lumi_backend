"""任务级、与运行时后端无关的执行状态机。

The orchestration package freezes *what* must happen into ``JobSpec``. This
module owns *how the complete graph converges*: dependency scheduling, bounded
concurrency, failure isolation, pause/cancel polling and a portable final
result. It does not know LLMs, Skills, Redis, PostgreSQL or Temporal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from lumi_orch.dag import validate_dag
from lumi_orch.job_spec import JobSpec, NodeSpec

from lumi_execution.ports import ExecutionControlPort, NodeLifecyclePort, TaskNodeExecutor
from lumi_execution.task_results import (
    FAILED_DEPENDENCY_STATUSES,
    STOPPED_JOB_STATUSES,
    TERMINAL_NODE_STATUSES,
    JobExecutionResult,
    JobExecutionStatus,
    NodeExecutionResult,
    build_job_result,
)


class TaskExecutionEngine:
    """Run one immutable JobSpec through ports and return the task outcome.

    Application adapters persist lifecycle updates, obtain distributed locks,
    journal external effects and invoke concrete workers. The core owns only
    deterministic graph progression and never imports application modules.
    """

    def __init__(
        self,
        *,
        executor: TaskNodeExecutor,
        concurrency: int = 1,
        control: ExecutionControlPort | None = None,
        lifecycle: NodeLifecyclePort | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._executor = executor
        self._concurrency = max(1, int(concurrency))
        self._control = control
        self._lifecycle = lifecycle
        self._poll_interval_seconds = max(0.01, float(poll_interval_seconds))

    async def run(
        self,
        spec: JobSpec,
        *,
        prior_results: tuple[NodeExecutionResult, ...] = (),
    ) -> JobExecutionResult:
        """Run all dependency-reachable nodes until terminal or suspended."""
        validate_dag(list(spec.nodes))
        nodes = {node.id: node for node in spec.nodes}
        results = self._initial_results(spec, prior_results)
        pending = {node.id for node in spec.nodes if node.id not in results}
        running: dict[str, asyncio.Task[NodeExecutionResult]] = {}

        while pending or running:
            control_status = await self._control_status(spec.job_id)
            if control_status in {"cancelled", "interrupted"}:
                running_ids = tuple(running)
                await self._cancel_running(running)
                for node_id in running_ids:
                    result = NodeExecutionResult(
                        node_id=node_id,
                        status="interrupted",
                        error="任务被用户终止",
                        error_code="INTERRUPTED",
                    )
                    results[node_id] = result
                    await self._notify(spec, nodes[node_id], result)
                for node_id in pending:
                    result = NodeExecutionResult(
                        node_id=node_id,
                        status="interrupted",
                        error="任务被用户终止",
                        error_code="INTERRUPTED",
                    )
                    results[node_id] = result
                    await self._notify(spec, nodes[node_id], result)
                return self._finish(spec, results, control_status)
            if control_status == "paused":
                if not running:
                    return self._finish(spec, results, "paused")
                await self._collect_finished(running, results, nodes, spec)
                await asyncio.sleep(self._poll_interval_seconds)
                continue

            ready, skipped = self._decide_ready(nodes, pending, results)
            for node_id in skipped:
                pending.remove(node_id)
                result = NodeExecutionResult(
                    node_id=node_id,
                    status="skipped",
                    error="前置依赖失败",
                    error_code="DEPENDENCY_FAILED",
                )
                results[node_id] = result
                await self._notify(spec, nodes[node_id], result)

            available = self._concurrency - len(running)
            for node_id in ready[:max(0, available)]:
                pending.remove(node_id)
                node = nodes[node_id]
                await self._notify(spec, node, None, phase="ready")
                dependencies = {
                    dependency: results[dependency].result or {}
                    for dependency in node.depends_on
                    if dependency in results and results[dependency].status == "completed"
                }
                running[node_id] = asyncio.create_task(self._run_one(spec, node, dependencies))

            if not running:
                if pending:
                    for node_id in pending:
                        result = NodeExecutionResult(
                            node_id=node_id,
                            status="skipped",
                            error="任务依赖无法收敛",
                            error_code="DAG_NOT_CONVERGED",
                        )
                        results[node_id] = result
                        await self._notify(spec, nodes[node_id], result)
                    return self._finish(spec, results, "failed")
                break

            done, _ = await asyncio.wait(
                set(running.values()),
                timeout=self._poll_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue
            stopped = await self._collect_finished(running, results, nodes, spec)
            if stopped:
                await self._cancel_running(running)
                return self._finish(spec, results, stopped)
            # A critical node is an explicit graph-wide stop boundary. Other
            # failures remain isolated and are reported as a partial result.
            critical_failed = next(
                (node_id for node_id, value in results.items()
                 if value.status == "failed" and nodes[node_id].execution.critical),
                None,
            )
            if critical_failed:
                await self._cancel_running(running)
                for node_id in pending:
                    result = NodeExecutionResult(
                        node_id=node_id, status="skipped",
                        error=f"关键节点 {critical_failed} 失败，停止后续执行",
                        error_code="CRITICAL_DEPENDENCY_FAILED",
                    )
                    results[node_id] = result
                    await self._notify(spec, nodes[node_id], result)
                return self._finish(spec, results, "failed")

        return self._finish(spec, results, self._derive_status(results, spec))

    @staticmethod
    def _initial_results(
        spec: JobSpec,
        prior: tuple[NodeExecutionResult, ...],
    ) -> dict[str, NodeExecutionResult]:
        valid_ids = {node.id for node in spec.nodes}
        return {
            result.node_id: result
            for result in prior
            if result.node_id in valid_ids and result.status in TERMINAL_NODE_STATUSES
        }

    @staticmethod
    def _decide_ready(
        nodes: Mapping[str, NodeSpec],
        pending: set[str],
        results: Mapping[str, NodeExecutionResult],
    ) -> tuple[list[str], list[str]]:
        ready: list[str] = []
        skipped: list[str] = []
        for node_id in sorted(pending):
            node = nodes[node_id]
            dependencies = [results.get(dependency) for dependency in node.depends_on]
            continue_after_failure = bool((node.metadata or {}).get("continue_on_dependency_failure"))
            if not continue_after_failure and any(
                value is not None and value.status in FAILED_DEPENDENCY_STATUSES
                for value in dependencies
            ):
                skipped.append(node_id)
                continue
            if all(
                value is not None and (
                    value.status in TERMINAL_NODE_STATUSES if continue_after_failure
                    else value.status == "completed"
                )
                for value in dependencies
            ):
                ready.append(node_id)
        return ready, skipped

    async def _run_one(
        self,
        spec: JobSpec,
        node: NodeSpec,
        dependency_results: Mapping[str, dict],
    ) -> NodeExecutionResult:
        await self._notify(spec, node, None, phase="running")
        try:
            result = await self._executor.execute_node(spec, node, dependency_results)
            if result.node_id != node.id:
                return NodeExecutionResult(
                    node_id=node.id,
                    status="failed",
                    error="节点执行器返回了不匹配的节点结果",
                    error_code="NODE_RESULT_MISMATCH",
                )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return NodeExecutionResult(
                node_id=node.id,
                status="failed",
                error=str(exc) or "节点执行异常",
                error_code="EXECUTION_ENGINE_ADAPTER_ERROR",
            )

    @staticmethod
    def _result_from_task(node_id: str, task: asyncio.Task[NodeExecutionResult]) -> NodeExecutionResult:
        try:
            return task.result()
        except asyncio.CancelledError:
            return NodeExecutionResult(
                node_id=node_id,
                status="interrupted",
                error="任务被用户终止",
                error_code="INTERRUPTED",
            )

    async def _collect_finished(
        self,
        running: dict[str, asyncio.Task[NodeExecutionResult]],
        results: dict[str, NodeExecutionResult],
        nodes: Mapping[str, NodeSpec],
        spec: JobSpec,
    ) -> str | None:
        stopped: str | None = None
        for node_id, task in list(running.items()):
            if not task.done():
                continue
            running.pop(node_id)
            result = self._result_from_task(node_id, task)
            results[node_id] = result
            await self._notify(spec, nodes[node_id], result)
            if result.status in STOPPED_JOB_STATUSES:
                stopped = result.status
        return stopped

    async def _control_status(self, job_id: str) -> str:
        if self._control is None:
            return "running"
        return str(await self._control.get_status(job_id) or "running").lower()

    async def _notify(
        self,
        spec: JobSpec,
        node: NodeSpec,
        result: NodeExecutionResult | None,
        *,
        phase: str | None = None,
    ) -> None:
        if self._lifecycle is not None:
            await self._lifecycle.on_node_state(spec, node, phase or (result.status if result else "running"), result)

    @staticmethod
    async def _cancel_running(running: Mapping[str, asyncio.Task[NodeExecutionResult]]) -> None:
        tasks = list(running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _derive_status(results: Mapping[str, NodeExecutionResult], spec: JobSpec) -> JobExecutionStatus:
        values = [results.get(node.id) for node in spec.nodes]
        if any(value is None for value in values):
            return "failed"
        if all(value and value.status == "completed" for value in values):
            return "completed"
        if any(value and value.status == "interrupted" for value in values):
            return "interrupted"
        completed = any(value and value.status == "completed" for value in values)
        isolated_failure = any(
            value and value.status in FAILED_DEPENDENCY_STATUSES
            and spec_node.execution.failure_isolation
            for spec_node, value in zip(spec.nodes, values, strict=False)
        )
        return "partial" if completed and isolated_failure else "failed"

    @staticmethod
    def _finish(
        spec: JobSpec,
        results: Mapping[str, NodeExecutionResult],
        status: JobExecutionStatus | str,
    ) -> JobExecutionResult:
        return build_job_result(spec, results, status)


__all__ = ["JobExecutionResult", "NodeExecutionResult", "TaskExecutionEngine"]
