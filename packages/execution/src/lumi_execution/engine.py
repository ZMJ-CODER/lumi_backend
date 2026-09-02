"""单个任务节点的确定性执行、审查与恢复循环。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lumi_execution.ports import (
    AttemptHook,
    ExceptionClassifier,
    FailurePolicy,
    NodeExecutor,
    ReviewPort,
)
from lumi_execution.telemetry import ExecutionTimer, NodeExecutionMetrics, NullTelemetry, TelemetryPort


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Stable result returned by every execution backend."""

    success: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    retries: int = 0
    recovery: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None


class ExecutionEngine:
    """Run a node without knowing which runtime owns the surrounding DAG.

    Legacy asyncio and Temporal Activities can both use this class.  The
    engine deliberately contains no persistence or side-effect policy; those
    are enforced by the caller before/after invoking it.
    """

    def __init__(
        self,
        *,
        executor: NodeExecutor,
        node: Any,
        context: Any,
        review: ReviewPort,
        failure_policy: FailurePolicy,
        timeout_seconds: int,
        max_retries: int,
        effectful: bool = False,
        on_running: AttemptHook | None = None,
        on_retry: AttemptHook | None = None,
        classify_exception: ExceptionClassifier | None = None,
        telemetry: TelemetryPort | None = None,
    ) -> None:
        self.executor = executor
        self.node = node
        self.context = context
        self.review = review
        self.failure_policy = failure_policy
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.effectful = effectful
        self.on_running = on_running
        self.on_retry = on_retry
        self.classify_exception = classify_exception or (lambda exc: (None, None))
        self.telemetry = telemetry or NullTelemetry()

    async def run(self) -> ExecutionOutcome:
        timer = ExecutionTimer()
        attempt = 0
        while True:
            if self.on_running:
                await self.on_running(attempt)
            result, error, error_code, retryable = await self._execute_once()
            if error is None:
                verdict = await self.review.review(self.node, result or {}, self.context)
                if getattr(verdict, "approved", True):
                    outcome = ExecutionOutcome(True, dict(result or {}), retries=attempt)
                    await self._record_metrics(timer, attempt, success=True)
                    return outcome
                error = f"质检未通过: {getattr(verdict, 'feedback', '')}"
                error_code = "REVIEW_REJECTED"
                retryable = True

            decision = self.failure_policy.decide(
                error_code,
                error,
                retryable=retryable,
                effectful=self.effectful,
                alternatives_remaining=self._alternatives_remaining(result),
            )
            if attempt < self.max_retries and (decision.retry_same or decision.try_alternative):
                if decision.try_alternative:
                    metadata = getattr(self.node, "metadata", None)
                    if isinstance(metadata, dict):
                        self.node.metadata = dict(metadata)
                        self.node.metadata["tool_index"] = int(metadata.get("tool_index") or 0) + 1
                attempt += 1
                if self.on_retry:
                    await self.on_retry(attempt)
                continue
            outcome = ExecutionOutcome(
                False,
                dict(result) if isinstance(result, Mapping) else None,
                error=error,
                error_code=error_code,
                retries=attempt,
                recovery=dict(decision.recovery) if decision.recovery else None,
                escalation=dict(decision.escalation) if decision.escalation else None,
            )
            await self._record_metrics(timer, attempt, success=False)
            return outcome

    async def _record_metrics(self, timer: ExecutionTimer, retries: int, *, success: bool) -> None:
        node_id = str(getattr(self.node, "id", "node"))
        try:
            await self.telemetry.record(
                NodeExecutionMetrics(
                    node_id=node_id,
                    execution_seconds=timer.elapsed(),
                    retries=retries,
                    attributes={
                        "agent": str(getattr(self.node, "agent", "unknown")),
                        "success": success,
                    },
                )
            )
        except Exception:  # noqa: BLE001
            # Observability must never change execution semantics.
            return None

    async def _execute_once(self) -> tuple[dict[str, Any] | None, str | None, str | None, bool]:
        try:
            value = await asyncio.wait_for(
                self.executor.execute(self.node, self.context), timeout=self.timeout_seconds
            )
            result = dict(value or {})
            if result.get("success") is False:
                return (
                    result,
                    str(result.get("error") or "执行失败"),
                    str(result.get("error_code") or "EXEC_ERROR"),
                    bool(result.get("retryable")),
                )
            return result, None, None, False
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return None, f"执行超时（>{self.timeout_seconds}s）", "NODE_TIMEOUT", False
        except Exception as exc:  # noqa: BLE001
            code, message = self.classify_exception(exc)
            return None, message or str(exc) or "执行失败", code or "EXEC_ERROR", code not in {
                "MODEL_INSUFFICIENT_BALANCE", "MODEL_AUTH_ERROR", "MODEL_NOT_FOUND",
                "MODEL_CONFIG_ERROR", "MODEL_TOOL_CALL_UNSUPPORTED", "MODEL_PROVIDER_UNAVAILABLE",
                "MODEL_CONNECTION_ERROR", "MODEL_UNAVAILABLE",
            }

    def _alternatives_remaining(self, result: Mapping[str, Any] | None) -> bool:
        return bool(isinstance(result, Mapping) and result.get("use_next_tool"))
