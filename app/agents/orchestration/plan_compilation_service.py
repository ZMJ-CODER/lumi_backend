"""Submission-time plan normalization and compilation.

This service is intentionally stateless.  It turns a planner result into a
runtime-safe tree and can ask the planner for one constrained correction when
the deterministic compiler rejects the first result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from app.agents.orchestration.plan_context import PlanRequestContext


class PlanCompilationService:
    def __init__(
        self,
        *,
        workers: dict,
        plan_with_context: Callable[[PlanRequestContext], Awaitable[Any]],
    ) -> None:
        self._workers = workers
        self._plan_with_context = plan_with_context

    def _normalize_for_workers(
        self,
        nodes,
        request: str,
        *,
        preserve_dependencies: bool = False,
        adapt_workers: bool = True,
    ) -> None:
        from app.agents.orchestration.plan_normalizer import (
            adapt_unavailable_manifest_workers,
            prefer_atomic_steps,
            serialize_steps,
        )

        prefer_atomic_steps(nodes, request)
        if adapt_workers:
            adapt_unavailable_manifest_workers(nodes, self._workers)
        if not preserve_dependencies:
            serialize_steps(nodes)

    async def compile_with_feedback(
        self,
        tree,
        *,
        routing: dict,
        context: PlanRequestContext,
        user_role: str,
    ):
        """Compile a tree and permit one planner correction with violations."""
        from app.agents.orchestration.plan_compiler import CompileDecision, compile_plan

        async def compile_current(current_tree):
            result = await compile_plan(
                current_tree.nodes,
                scene=context.scene,
                user_role=user_role,
                user_id=context.user_id,
                workers=self._workers,
            )
            routing["plan_compiler"] = {
                "decision": result.decision.value,
                "capability_fingerprint": result.capabilities.fingerprint,
                "cost": result.cost.model_dump(mode="json"),
                "violations": [item.model_dump(mode="json") for item in result.violations[:8]],
                "warnings": [item.model_dump(mode="json") for item in result.warnings[:8]],
            }
            return result

        compiled = await compile_current(tree)
        if compiled.decision == CompileDecision.REPLAN_REQUIRED:
            feedback = "；".join(item.message for item in compiled.violations[:5])
            logger.warning(
                "计划编译器拒绝规划，触发一次带反馈重规划: user={} reason={}",
                context.user_id[:8], feedback[:300],
            )
            retry_summaries = (
                (context.prior_summaries + "\n") if context.prior_summaries else ""
            ) + (
                "上一版计划未通过执行前编译检查。请修正以下约束后重新输出完整 JSON，"
                "不要删除用户要求的动作：" + feedback[:1200]
            )
            try:
                revised = await self._plan_with_context(
                    context.with_prior_summaries(retry_summaries)
                )
            except Exception as exc:  # noqa: BLE001
                revised = None
                logger.warning("带反馈重规划失败: {}", exc)
            if revised is not None and revised.nodes and not revised.error:
                self._normalize_for_workers(revised.nodes, context.request)
                compiled = await compile_current(revised)
                tree = revised

        if compiled.decision == CompileDecision.REPLAN_REQUIRED:
            detail = "；".join(item.message for item in compiled.violations[:5])
            tree.error = "计划未通过执行前检查：" + detail[:500]
            tree.error_code = "PLAN_COMPILATION_ERROR"
            tree.nodes = []
            return tree
        tree.nodes = compiled.nodes
        return tree

    def normalize_for_submission(
        self, nodes, request: str, *, preserve_dependencies: bool = False
    ) -> None:
        self._normalize_for_workers(
            nodes, request, preserve_dependencies=preserve_dependencies
        )

    def normalize_for_replan(
        self,
        nodes,
        request: str,
        *,
        preserve_dependencies: bool = True,
        adapt_workers: bool = False,
    ) -> None:
        """Normalize a replacement graph without changing its planned shape.

        Failed-job replans can attach their roots to completed nodes from a
        previous revision.  They therefore retain planner dependencies unless
        the caller explicitly requests the rolling-plan serialization policy.
        """
        self._normalize_for_workers(
            nodes,
            request,
            preserve_dependencies=preserve_dependencies,
            adapt_workers=adapt_workers,
        )
