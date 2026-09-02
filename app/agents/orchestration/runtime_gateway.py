"""编排门面的执行运行时适配器。

本模块负责后端选择与 Temporal 提交细节；它不决定计划，也不执行节点，这些仍
分别由编译器与执行器负责。
"""

from __future__ import annotations

import hashlib
import time

from loguru import logger

from app.agents.orchestration.models import Job
from app.agents.orchestration.state import StateStore
from app.core.config import settings


class RuntimeGateway:
    def __init__(self, *, store: StateStore, temporal_mode: bool):
        self.store = store
        self.temporal_mode = temporal_mode
        self.temporal_available = False
        self.temporal_probe_at = 0.0
        self.temporal_unavailable_until = 0.0

    async def probe_temporal(self) -> bool:
        if not self.temporal_mode:
            return False
        if self.temporal_available:
            return True
        now = time.monotonic()
        if now < self.temporal_unavailable_until:
            return False
        if now - self.temporal_probe_at < 30:
            return False
        self.temporal_probe_at = now
        try:
            from app.agents.orchestration.temporal.client import get_temporal_client

            await get_temporal_client()
            self.temporal_available = True
            self.temporal_unavailable_until = 0.0
            logger.info("Temporal 已连接: {}", settings.TEMPORAL_ADDRESS)
        except Exception as exc:  # noqa: BLE001
            self.temporal_unavailable_until = now + 300
            logger.warning("Temporal 不可用（{}），多智能体任务回退自建 DAG", exc)
        return self.temporal_available

    async def submit_static(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        """Submit a safe, static DAG to the external Temporal worker."""
        from app.agents.orchestration.temporal.client import (
            start_agent_workflow,
            store_job_llm_config,
            store_byok_key,
            store_temporal_replan_context,
        )
        from app.agents.orchestration.job_contract import freeze_job_spec

        job.routing = {
            **(job.routing or {}),
            "runtime": "temporal_static",
            "runtime_version": 4,
            "temporal_static_eligibility": (job.routing or {}).get(
                "temporal_static_eligibility",
                {"eligible": True, "code": "eligible", "detail": "静态、完全物化 DAG"},
            ),
        }
        spec = freeze_job_spec(job)
        payload = job.model_dump()
        # The mutable Job snapshot remains available for status presentation;
        # the spec is the immutable execution contract for the Workflow.
        payload["execution_spec"] = spec.model_dump(mode="json")
        payload["config"] = {
            "node_timeout_seconds": settings.AGENT_NODE_TIMEOUT_SECONDS,
            "node_max_retries": settings.AGENT_NODE_MAX_RETRIES,
            "node_concurrency": settings.AGENT_NODE_CONCURRENCY,
            "activity_heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
            "static_max_replans": settings.TEMPORAL_STATIC_MAX_REPLANS,
            "long_dag": self.is_long_static_job(job),
            "use_node_child_workflows": bool(settings.TEMPORAL_STATIC_CHILD_WORKFLOW_ENABLED),
            "continue_as_new_after_nodes": settings.TEMPORAL_STATIC_CONTINUE_AS_NEW_AFTER_NODES,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        # Replanning input is stored outside history.  It is intentionally
        # limited to the submission context and never contains a BYOK key.
        try:
            await store_temporal_replan_context(
                job.job_id,
                {
                    "user_id": job.user_id,
                    "request": job.request,
                    "scene": job.scene,
                    "project_id": (job.routing or {}).get("project_id"),
                    "project_ids": (job.routing or {}).get("project_ids") or [],
                    "office_docs": (job.routing or {}).get("input_refs") or [],
                    "prior_summaries": "",
                    "routing": {
                        "level": (job.routing or {}).get("level", "m2"),
                        "plan_revision": (job.routing or {}).get("plan_revision", 1),
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            # The ordinary static Workflow can still execute without automatic
            # replanning.  Do not make an optional recovery capability a new
            # submission dependency; a future failed node records the reason.
            job.routing = {
                **(job.routing or {}),
                "temporal_replan_context_error": str(exc)[:160],
            }
            logger.warning("Temporal 重规划上下文未保存，自动重规划将不可用: {}", exc)
        await start_agent_workflow(payload, job.job_id)

    async def submit_manifest(self, job: Job, llm_api_key: str | None, llm_config: dict | None = None) -> None:
        """Persist a compact job reference and start the rolling manifest workflow."""
        from app.agents.orchestration.temporal.client import start_manifest_workflow, store_job_llm_config, store_byok_key

        job.routing = {
            **(job.routing or {}),
            "runtime": "manifest_temporal",
            "runtime_version": 1,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_manifest_workflow(
            {
                "job_id": job.job_id,
                "heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
                "batch_timeout_seconds": max(
                    300,
                    int(settings.AGENT_NODE_TIMEOUT_SECONDS)
                    * max(2, int((job.routing.get("manifest") or {}).get("batch_size") or 1)),
                ),
                "continue_after_batches": settings.TEMPORAL_MANIFEST_CONTINUE_AS_NEW_BATCHES,
            },
            job.job_id,
        )

    async def submit_logical_read(
        self,
        job: Job,
        llm_api_key: str | None,
        llm_config: dict | None = None,
    ) -> None:
        """启动仅推进已持久化纯读前沿的紧凑 Workflow。

        完整逻辑计划、节点正文和结果均保留在 Redis；进入 History 的仅有
        ``job_id`` 和确定性的生命周期限制。
        """
        from app.agents.orchestration.temporal.client import (
            start_logical_read_workflow,
            store_byok_key,
            store_job_llm_config,
            store_temporal_replan_context,
        )

        job.routing = {
            **(job.routing or {}),
            "runtime": "temporal_logical_read",
            "runtime_version": 1,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        # 受限重规划 Activity 需要与初始计划同一份授权文档范围，但该上下文
        # 不能进入 Workflow History。保存失败时，任务仍能执行；仅失败后的
        # 自动替代会被安全地拒绝。
        try:
            await store_temporal_replan_context(
                job.job_id,
                {
                    "user_id": job.user_id,
                    "request": job.request,
                    "scene": job.scene,
                    "project_id": (job.routing or {}).get("project_id"),
                    "project_ids": (job.routing or {}).get("project_ids") or [],
                    "office_docs": (job.routing or {}).get("input_refs") or [],
                    "prior_summaries": "",
                },
            )
        except Exception as exc:  # noqa: BLE001
            job.routing = {
                **(job.routing or {}),
                "temporal_logical_read_replan_context_error": str(exc)[:160],
            }
            logger.warning("Temporal 纯读逻辑计划重规划上下文未保存: {}", exc)
        await start_logical_read_workflow(
            {
                "job_id": job.job_id,
                "heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
                "frontier_timeout_seconds": max(
                    300,
                    int(settings.AGENT_NODE_TIMEOUT_SECONDS)
                    * max(1, int(settings.AGENT_LOGICAL_PLAN_FRONTIER_SIZE)),
                ),
                "continue_after_frontiers": settings.TEMPORAL_LOGICAL_READ_CONTINUE_AFTER_FRONTIERS,
            },
            job.job_id,
        )

    async def submit_logical_effects(
        self,
        job: Job,
        llm_api_key: str | None,
        llm_config: dict | None = None,
    ) -> None:
        """启动预声明审批副作用的逻辑计划 Workflow。"""
        from app.agents.orchestration.temporal.client import (
            start_logical_effects_workflow,
            store_byok_key,
            store_job_llm_config,
        )

        job.routing = {
            **(job.routing or {}),
            "runtime": "temporal_logical_effects",
            "runtime_version": 1,
        }
        await self.store.create_job(job)
        if llm_config:
            await store_job_llm_config(job.job_id, llm_config)
        elif llm_api_key:
            await store_byok_key(job.job_id, llm_api_key)
        await start_logical_effects_workflow(
            {
                "job_id": job.job_id,
                "heartbeat_seconds": settings.TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS,
                "frontier_timeout_seconds": max(
                    300,
                    int(settings.AGENT_NODE_TIMEOUT_SECONDS)
                    * max(1, int(settings.AGENT_LOGICAL_PLAN_FRONTIER_SIZE)),
                ),
                "continue_after_frontiers": settings.TEMPORAL_LOGICAL_EFFECTS_CONTINUE_AFTER_FRONTIERS,
            },
            job.job_id,
        )

    async def _signal_manifest(self, job_id: str, signal: str, arg=None) -> None:
        from app.agents.orchestration.temporal.client import signal_manifest_workflow

        await signal_manifest_workflow(job_id, signal, arg)

    async def cancel_manifest(self, job_id: str, keep_completed: bool = True) -> None:
        await self._signal_manifest(job_id, "cancel_request", keep_completed)

    async def pause_manifest(self, job_id: str) -> None:
        await self._signal_manifest(job_id, "pause")

    async def resume_manifest(self, job_id: str) -> None:
        await self._signal_manifest(job_id, "resume")

    @staticmethod
    def can_run_manifest(job: Job) -> bool:
        manifest = (job.routing or {}).get("manifest")
        if not isinstance(manifest, dict):
            return False
        for item in list(manifest.get("items") or []):
            route = str(item.get("route") or item.get("estimated_type") or "")
            if route not in {"direct_llm", "rag"} or list(item.get("subtasks") or []):
                return False
        return True

    @staticmethod
    def can_run_static(job: Job) -> bool:
        """Return whether a job preserves all semantics in the static worker.

        Dynamic replanning, ReAct, and unapproved writes retain the legacy
        runtime; only allow-listed approval-gated static effects are admitted.
        """
        from app.agents.orchestration.temporal_policy import evaluate_static_temporal

        decision = evaluate_static_temporal(
            job,
            max_nodes=settings.TEMPORAL_STATIC_MAX_NODES,
            long_dag_enabled=settings.TEMPORAL_STATIC_LONG_DAG_ENABLED,
            long_dag_max_nodes=settings.TEMPORAL_STATIC_LONG_DAG_MAX_NODES,
        )
        if isinstance(job.routing, dict):
            job.routing["temporal_static_eligibility"] = {
                "eligible": decision.eligible,
                "code": decision.code,
                "detail": decision.detail,
            }
        return decision.eligible

    @staticmethod
    def static_eligibility(job: Job):
        """Return an auditable Temporal admission decision for this Job."""
        from app.agents.orchestration.temporal_policy import evaluate_static_temporal

        return evaluate_static_temporal(
            job,
            max_nodes=settings.TEMPORAL_STATIC_MAX_NODES,
            long_dag_enabled=settings.TEMPORAL_STATIC_LONG_DAG_ENABLED,
            long_dag_max_nodes=settings.TEMPORAL_STATIC_LONG_DAG_MAX_NODES,
        )

    @staticmethod
    def is_long_static_job(job: Job) -> bool:
        """Whether this admitted Job uses the pure-read long-DAG runtime path."""
        return (
            bool(settings.TEMPORAL_STATIC_LONG_DAG_ENABLED)
            and len(job.nodes) > max(1, int(settings.TEMPORAL_STATIC_MAX_NODES))
        )

    @staticmethod
    def static_rollout_eligibility(job: Job):
        """Apply deterministic canary controls after capability eligibility.

        An allowlist is explicit rollout authority.  All other tasks use a
        stable job-id bucket, so a retry never changes backend merely because
        the process or wall-clock time changed.
        """
        from app.agents.orchestration.temporal_policy import StaticTemporalDecision

        capability = RuntimeGateway.static_eligibility(job)
        if not capability.eligible:
            return capability
        allowlist = {
            value.strip() for value in settings.TEMPORAL_STATIC_ALLOWLIST.split(",") if value.strip()
        }
        if str(job.user_id) in allowlist:
            return capability
        if allowlist:
            return StaticTemporalDecision(False, "rollout_not_allowlisted", "用户不在 Temporal 灰度白名单")
        allowed_types = {
            value.strip() for value in settings.TEMPORAL_STATIC_TASK_TYPES.split(",") if value.strip()
        }
        if allowed_types:
            task_types = {
                str(node.agent) for node in job.nodes
            } | {
                str((node.metadata or {}).get("route_channel") or "") for node in job.nodes
            }
            task_types.discard("")
            if not task_types.issubset(allowed_types):
                return StaticTemporalDecision(False, "rollout_task_type", "任务类型不在 Temporal 灰度范围")
        percentage = max(0, min(100, int(settings.TEMPORAL_STATIC_PERCENTAGE)))
        if percentage <= 0:
            return StaticTemporalDecision(False, "rollout_disabled", "Temporal 静态任务灰度比例为 0")
        bucket = int(hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket >= percentage:
            return StaticTemporalDecision(False, "rollout_percentage", f"任务未命中 {percentage}% Temporal 灰度桶")
        return capability

    @staticmethod
    def logical_read_rollout_eligibility(job: Job, plan: dict | None):
        """Evaluate capability and deterministic canary controls for logical reads."""
        from app.agents.orchestration.temporal_policy import (
            StaticTemporalDecision,
            evaluate_logical_read_temporal,
        )

        capability = evaluate_logical_read_temporal(job, plan)
        if not capability.eligible:
            return capability
        allowlist = {
            value.strip() for value in settings.TEMPORAL_LOGICAL_READ_ALLOWLIST.split(",") if value.strip()
        }
        if str(job.user_id) in allowlist:
            return capability
        if allowlist:
            return StaticTemporalDecision(False, "rollout_not_allowlisted", "用户不在逻辑计划 Temporal 灰度白名单")
        allowed_types = {
            value.strip() for value in settings.TEMPORAL_LOGICAL_READ_TASK_TYPES.split(",") if value.strip()
        }
        if allowed_types and isinstance(plan, dict):
            types = {
                str(((record or {}).get("node") or {}).get("agent") or "")
                for record in (plan.get("nodes") or {}).values()
                if isinstance(record, dict)
            }
            if not types.issubset(allowed_types):
                return StaticTemporalDecision(False, "rollout_task_type", "任务类型不在逻辑计划 Temporal 灰度范围")
        percentage = max(0, min(100, int(settings.TEMPORAL_LOGICAL_READ_PERCENTAGE)))
        if percentage <= 0:
            return StaticTemporalDecision(False, "rollout_disabled", "Temporal 逻辑计划灰度比例为 0")
        bucket = int(hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket >= percentage:
            return StaticTemporalDecision(False, "rollout_percentage", f"任务未命中 {percentage}% Temporal 逻辑计划灰度桶")
        return capability

    @staticmethod
    def logical_effects_rollout_eligibility(job: Job, plan: dict | None):
        """对预声明审批副作用逻辑计划应用稳定灰度策略。"""
        from app.agents.orchestration.temporal_policy import (
            StaticTemporalDecision,
            evaluate_logical_effect_temporal,
        )

        capability = evaluate_logical_effect_temporal(job, plan)
        if not capability.eligible:
            return capability
        allowlist = {
            value.strip() for value in settings.TEMPORAL_LOGICAL_EFFECTS_ALLOWLIST.split(",") if value.strip()
        }
        if str(job.user_id) in allowlist:
            return capability
        if allowlist:
            return StaticTemporalDecision(False, "rollout_not_allowlisted", "用户不在逻辑计划副作用灰度白名单")
        allowed_types = {
            value.strip() for value in settings.TEMPORAL_LOGICAL_EFFECTS_TASK_TYPES.split(",") if value.strip()
        }
        if allowed_types and isinstance(plan, dict):
            types = {
                str(((record or {}).get("node") or {}).get("agent") or "")
                for record in (plan.get("nodes") or {}).values()
                if isinstance(record, dict)
            }
            if not types.issubset(allowed_types):
                return StaticTemporalDecision(False, "rollout_task_type", "任务类型不在逻辑计划副作用灰度范围")
        percentage = max(0, min(100, int(settings.TEMPORAL_LOGICAL_EFFECTS_PERCENTAGE)))
        if percentage <= 0:
            return StaticTemporalDecision(False, "rollout_disabled", "Temporal 逻辑计划副作用灰度比例为 0")
        bucket = int(hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket >= percentage:
            return StaticTemporalDecision(False, "rollout_percentage", f"任务未命中 {percentage}% Temporal 逻辑计划副作用灰度桶")
        return capability

    @staticmethod
    def is_manifest_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "manifest_temporal")

    @staticmethod
    def is_static_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "temporal_static")

    @staticmethod
    def is_logical_read_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "temporal_logical_read")

    @staticmethod
    def is_logical_effects_job(job: Job | None) -> bool:
        return bool(job and str((job.routing or {}).get("runtime") or "") == "temporal_logical_effects")
