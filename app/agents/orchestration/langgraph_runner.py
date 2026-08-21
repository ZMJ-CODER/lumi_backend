"""办公节点的 LangGraph 执行图。

任务之间的依赖、资源锁和持久化仍属于编排基础设施；单个原子节点内部的
执行、审查、重试与换方法则统一由 LangGraph 表达。这样两种运行时
（本地 DAG / Temporal Activity）共享完全相同的恢复语义。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from loguru import logger

from app.agents.skills.recovery import decide_failure


class NodeGraphState(TypedDict, total=False):
    attempt: int
    result: dict | None
    error: str | None
    error_code: str | None
    retryable: bool
    action: Literal["retry", "finish"]
    succeeded: bool
    recovery: dict[str, Any]
    escalation: dict[str, Any]


@dataclass(frozen=True)
class NodeGraphOutcome:
    success: bool
    result: dict | None = None
    error: str | None = None
    error_code: str | None = None
    retries: int = 0
    recovery: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None


AttemptHook = Callable[[int], Awaitable[None]]


class LangGraphNodeRunner:
    """用状态图封装一个节点的 execute → review → recover 循环。

    ``on_running`` / ``on_retry`` 保持 UI 快照及时更新；Worker 和资源锁仍由
    调用者注入，因而不会绕过既有的权限、幂等或资源互斥边界。
    """

    def __init__(
        self,
        *,
        worker,
        node,
        ctx,
        review,
        timeout_seconds: int,
        max_retries: int,
        effectful: bool = False,
        on_running: AttemptHook | None = None,
        on_retry: AttemptHook | None = None,
    ) -> None:
        self.worker = worker
        self.node = node
        self.ctx = ctx
        self.review = review
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.effectful = effectful
        self.on_running = on_running
        self.on_retry = on_retry

    async def run(self) -> NodeGraphOutcome:
        graph = StateGraph(NodeGraphState)
        graph.add_node("execute", self._execute)
        graph.add_node("assess", self._assess)
        graph.add_node("retry", self._retry)
        graph.add_edge(START, "execute")
        graph.add_edge("execute", "assess")
        graph.add_conditional_edges("assess", self._route, {"retry": "retry", "finish": END})
        graph.add_edge("retry", "execute")
        state = await graph.compile().ainvoke({"attempt": 0})
        return NodeGraphOutcome(
            success=bool(state.get("succeeded")),
            result=state.get("result"),
            error=state.get("error"),
            error_code=state.get("error_code"),
            retries=int(state.get("attempt") or 0),
            recovery=state.get("recovery"),
            escalation=state.get("escalation"),
        )

    async def _execute(self, state: NodeGraphState) -> dict:
        if self.on_running:
            await self.on_running(int(state.get("attempt") or 0))
        started = time.perf_counter()
        succeeded = False
        try:
            result = await asyncio.wait_for(
                self.worker.execute(self.node, self.ctx), timeout=self.timeout_seconds
            )
            logger.info(
                "办公节点执行耗时: job={} node={} agent={} attempt={} duration_ms={}",
                self.ctx.job_id[:8],
                self.node.id,
                self.node.agent,
                int(state.get("attempt") or 0),
                int((time.perf_counter() - started) * 1000),
            )
            if isinstance(result, dict) and result.get("success") is False:
                return {
                    "result": result,
                    "error": str(result.get("error") or "执行失败"),
                    "error_code": str(result.get("error_code") or "EXEC_ERROR"),
                    "retryable": bool(result.get("retryable")),
                }
            succeeded = True
            return {"result": result or {}, "error": None, "error_code": None, "retryable": False}
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "办公节点执行超时: job={} node={} agent={} timeout_s={}",
                self.ctx.job_id[:8], self.node.id, self.node.agent, self.timeout_seconds,
            )
            return {
                "result": None,
                "error": f"执行超时（>{self.timeout_seconds}s）",
                "error_code": "TIMEOUT",
                "retryable": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "办公节点执行异常: job={} node={} agent={} duration_ms={} error={}",
                self.ctx.job_id[:8], self.node.id, self.node.agent,
                int((time.perf_counter() - started) * 1000), exc,
            )
            return {
                "result": None,
                "error": str(exc) or "执行失败",
                "error_code": "EXEC_ERROR",
                "retryable": True,
            }
        finally:
            try:
                from app.core.observability import observe_agent_node_duration

                observe_agent_node_duration(
                    self.node.agent,
                    succeeded,
                    time.perf_counter() - started,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _assess(self, state: NodeGraphState) -> dict:
        result = state.get("result")
        error = state.get("error")
        error_code = state.get("error_code")
        retryable = bool(state.get("retryable"))

        if not error:
            verdict = await self.review.review(self.node, result or {}, self.ctx)
            if verdict.approved:
                return {"succeeded": True, "action": "finish"}
            error = f"质检未通过: {verdict.feedback}"
            error_code = "REVIEW_REJECTED"
            # 质检失败属于纯计算重做；副作用节点仍由 effectful 规则阻止重放。
            retryable = True

        decision = decide_failure(
            error_code,
            error,
            retryable=retryable,
            effectful=self.effectful,
            alternatives_remaining=bool(isinstance(result, dict) and result.get("use_next_tool")),
        )
        recovery = {
            "category": decision.category,
            "replan_required": decision.replan_required,
            "user_action_required": decision.user_action_required,
            "switched_tool": decision.try_alternative,
        }
        # The worker may emit an explicit L2/L3 protocol object.  Otherwise
        # preserve existing Skill recovery semantics through a conservative
        # compatibility mapping.  This data only leaves the node; it does not
        # grant the worker permission to mutate the enclosing DAG.
        from app.agents.orchestration.escalation import coerce_escalation, infer_escalation

        explicit = result.get("escalation") if isinstance(result, dict) else None
        signal = coerce_escalation(explicit, default_node_id=self.node.id) or infer_escalation(
            error_code=error_code,
            recovery=recovery,
            message=str(error or ""),
            node_id=self.node.id,
        )
        escalation = signal.model_dump(mode="json") if signal else None
        attempt = int(state.get("attempt") or 0)
        may_retry = attempt < self.max_retries and (decision.retry_same or decision.try_alternative)
        if may_retry:
            if decision.try_alternative:
                self.node.metadata = dict(self.node.metadata or {})
                self.node.metadata["tool_index"] = int(self.node.metadata.get("tool_index") or 0) + 1
            return {
                "error": error,
                "error_code": error_code,
                "recovery": recovery,
                "escalation": escalation,
                "action": "retry",
                "succeeded": False,
            }
        return {
            "error": error,
            "error_code": error_code,
            "recovery": recovery,
            "escalation": escalation,
            "action": "finish",
            "succeeded": False,
        }

    async def _retry(self, state: NodeGraphState) -> dict:
        next_attempt = int(state.get("attempt") or 0) + 1
        if self.on_retry:
            await self.on_retry(next_attempt)
        return {"attempt": next_attempt, "error": None, "error_code": None, "retryable": False}

    @staticmethod
    def _route(state: NodeGraphState) -> Literal["retry", "finish"]:
        return "retry" if state.get("action") == "retry" else "finish"
