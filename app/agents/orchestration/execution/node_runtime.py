"""独立节点执行引擎的 Lumi 应用适配器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.agents.skills.recovery import decide_failure
from lumi_execution import DirectExecutionRuntime, FailureDecision, TelemetryPort


from lumi_execution import ExecutionOutcome

NodeGraphOutcome = ExecutionOutcome


AttemptHook = Callable[[int], Awaitable[None]]


class NodeExecutionRunner:
    """Lumi adapter backed by ``lumi_execution.ExecutionEngine``."""

    def __init__(self, *, worker, node, ctx, review, timeout_seconds: int, max_retries: int,
                 effectful: bool = False, on_running: AttemptHook | None = None,
                 on_retry: AttemptHook | None = None,
                 telemetry: TelemetryPort | None = None) -> None:
        self.worker = worker
        self.node = node
        self.ctx = ctx
        self.review = review
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.effectful = effectful
        self.on_running = on_running
        self.on_retry = on_retry
        self.telemetry = telemetry

    async def run(self) -> NodeGraphOutcome:
        outer = self

        class WorkerAdapter:
            async def execute(self, node, context):
                result = await outer.worker.execute(node, context)
                return result or {}

        class RecoveryPolicy:
            def decide(self, error_code, error, *, retryable, effectful, alternatives_remaining):
                decision = decide_failure(error_code, error, retryable=retryable,
                                          effectful=effectful,
                                          alternatives_remaining=alternatives_remaining)
                recovery = {"category": decision.category,
                            "replan_required": decision.replan_required,
                            "user_action_required": decision.user_action_required,
                            "switched_tool": decision.try_alternative}
                from app.agents.orchestration.escalation import infer_escalation
                signal = infer_escalation(error_code=error_code, recovery=recovery,
                                          message=str(error or ""), node_id=outer.node.id)
                return FailureDecision(retry_same=decision.retry_same,
                                       try_alternative=decision.try_alternative,
                                       category=decision.category,
                                       replan_required=decision.replan_required,
                                       user_action_required=decision.user_action_required,
                                       recovery=recovery,
                                       escalation=signal.model_dump(mode="json") if signal else None)

        outcome = await DirectExecutionRuntime().run_node(
            executor=WorkerAdapter(), node=self.node, context=self.ctx, review=self.review,
            failure_policy=RecoveryPolicy(), timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries, effectful=self.effectful,
            on_running=self.on_running, on_retry=self.on_retry,
            classify_exception=self._classify_model_exception,
            telemetry=self.telemetry,
        )
        return NodeGraphOutcome(success=outcome.success, result=outcome.result,
                                error=outcome.error, error_code=outcome.error_code,
                                retries=outcome.retries, recovery=outcome.recovery,
                                escalation=outcome.escalation)

    def _classify_model_exception(self, exc: Exception) -> tuple[str | None, str | None]:
        try:
            from app.agents.skills.recovery import classify_model_error
            code, message = classify_model_error(exc)
            if code != "MODEL_UNAVAILABLE" or getattr(self.ctx, "llm_config", None):
                return code, message
        except Exception:  # noqa: BLE001
            pass
        return None, None
